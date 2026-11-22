"""DemoHarness 内核 · 会话拥有者（core/session.py）。

会话拥有者：每个会话一个 FIFO，串行化对该会话的一切写入；所有动词
（append/reset/compact/delete/fork/set_model）都投给目标会话的 FIFO 执行。
分层原则：本层组装业务语义（什么是 reset、重放怎么走），SQL 一律交给
storage.py。

核心模型（见 docs/design/storage-session.md）：

- 不再有全局 FIFO。一切操作投给目标会话的 FIFO；fork 走源会话 FIFO
  （读源必须串行在源的写入之后）；delete 走会话树中每个会话的协调。
- 跨会话一致性靠 SQLite 单写者 + BEGIN IMMEDIATE：所有写事务天然互斥。
  fork 的「读源链 + 写新会话」放进一个写事务，源 append 被写锁挡住，
  快照语义由写锁保证，无需显式锁。
- 重放是纯内存函数，从 storage.entries() 的 id 序建链，不再额外发 SQL。

running 状态机（本模块定死，登记方是 task.py / loop.py）：

    登记 = 任务输入的 AppendOp 提交成功后（调用方调 mark_running）
    清除 = Task 终态（stop_reason 落盘）后（调用方调 clear_running）
    不变式 = running_task(sid) 非空 ⟺ 该会话有任务在输入已落盘、终态未落盘之间

mark_running / clear_running 是公开方法兼测试钩子：测试可直接调模拟忙碌，
无需依赖 task.py。

第一阶段契约冻结（T01）：NewOp 带齐后台三列（background /
origin_parent_id / restart_count），CompactOp 的 running 守卫与
COMPACTION_STARTED publish 点在本模块，SetModelOp 让会话层模型覆盖经
会话 FIFO 写入（不再由门面直接发 SQL，见架构不变量 1）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from .events import EventBus, compaction_started_event
from .storage import EntryRow, HistoryEntry, SessionRow, Storage
from .task import Action
from .tools import model_tool_call

# entry kind 约定（唯一显著定义处；多 Agent / 后台任务会继续扩展）。
INTERRUPTED_KIND = "interrupted"
BOUNDARY_KINDS = ("compaction", "reset")
# 验证失败的纠正证据：由 loop.py 在 finish_turn 判失败时落盘，下一轮重放
# 自然携带给模型自纠（不新增第七态，见 architecture.md 六态闭表）。
VERIFICATION_FAILED_KIND = "verification_failed"


class SessionNotFound(Exception):
    """会话不存在或已被删除（tombstone 后）仍提交操作时触发。"""


class SessionBusy(Exception):
    """会话正在运行任务（running 非空）或队列非空，动词守卫拒绝时触发。"""


class InvalidEntry(Exception):
    """fork/reset 传入的 entry id 不是本会话树内的合法锚点。"""


class StorageClosed(Exception):
    """Storage 已 close 后仍投操作时触发。"""


@dataclass(frozen=True)
class SessionOp:
    session_id: str


@dataclass(frozen=True)
class NewOp(SessionOp):
    bot_id: str
    model: str | None = None
    title: str | None = None
    # 后台任务三列（第 8 步编排消费；本轮冻结形状，不实现编排）。
    background: bool = False
    origin_parent_id: str | None = None
    restart_count: int = 0


@dataclass(frozen=True)
class AppendOp(SessionOp):
    kind: str
    content: str
    parent: str | None
    # 任务归属：任务内的 append 都带 task_id（供 running_task 查询与审计）。
    task_id: str | None = None


@dataclass(frozen=True)
class SetModelOp(SessionOp):
    """会话层模型覆盖：config 三层链的「会话」层写入动词（经 FIFO 串行）。"""

    model: str | None


@dataclass(frozen=True)
class ForkOp(SessionOp):
    at: str | None


@dataclass(frozen=True)
class ResetOp(SessionOp):
    to: str


@dataclass(frozen=True)
class CompactOp(SessionOp):
    """压缩提交形状（两段式的第二段）：事务外生成摘要 → 事务内校验 leaf 后落盘。

    `during_task=True` 用于 loop 在任务边界触发的压缩——此时 running_task 非空
    但 FIFO 空闲、无并发 append，允许提交（区别于手动对空闲会话的 compact）。
    """

    summary: str
    leaf: str | None = None
    during_task: bool = False


@dataclass(frozen=True)
class DeleteOp(SessionOp):
    pass


@dataclass(frozen=True)
class HistoryOp(SessionOp):
    pass


@dataclass
class SessionInfo:
    id: str
    bot_id: str
    parent_id: str | None
    active_entry_id: str | None
    running_task_id: str | None = None
    model: str | None = None
    title: str | None = None
    created_at: float | None = None
    background: bool = False
    restart_count: int = 0


# ── 重放：纯内存函数 ──────────────────────────────────────────────


def replay_entries(all_entries: list[EntryRow], leaf_id: str | None) -> list[EntryRow]:
    """模型上下文的唯一来源（读路径）。

    从活跃叶沿 parent 链向老回溯，遇到第一条边界 entry（compaction / reset）
    即停；返回按时间序 [边界 entry, ..., leaf]。leaf 为 None（空会话）返回 []。
    """
    if leaf_id is None:
        return []
    by_id = {e.id: e for e in all_entries}
    chain: list[EntryRow] = []
    cur = by_id.get(leaf_id)
    while cur is not None:
        chain.append(cur)
        if cur.kind in BOUNDARY_KINDS:
            break
        cur = by_id.get(cur.parent_id) if cur.parent_id is not None else None
    chain.reverse()
    return chain


def _tree_sessions(all_sessions: list[SessionRow], root_id: str) -> list[str]:
    """收集根会话 + 全部 fork 子树会话 id（parent_id 边）。"""
    by_parent: dict[str | None, list[str]] = {}
    for s in all_sessions:
        by_parent.setdefault(s.parent_id, []).append(s.id)
    out: list[str] = []
    stack = [root_id]
    while stack:
        sid = stack.pop()
        out.append(sid)
        stack.extend(by_parent.get(sid, []))
    return out


class SessionManager:
    def __init__(self, storage: Storage, event_bus: EventBus | None = None):
        self._storage = storage
        # 可选事件总线：core 内部只做活体推送（不变量 6），未注入时静默跳过。
        self._event_bus = event_bus
        # 内存态：会话 id -> 队列 (list) 与 worker 存在标记。
        self._queues: dict[str, list[tuple[SessionOp, asyncio.Future]]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        # 内存态 tombstone：delete 流程打标，删后提交抛 SessionNotFound。
        self._tombstoned: set[str] = set()
        # running 登记：会话 id -> task_id。
        self._running: dict[str, str] = {}

    # ── running 登记（守卫信号，兼测试钩子） ──────────────────────

    def mark_running(self, sid: str, task_id: str) -> None:
        self._running[sid] = task_id

    def clear_running(self, sid: str) -> None:
        self._running.pop(sid, None)

    def running_task(self, sid: str) -> str | None:
        return self._running.get(sid)

    # ── 提交流程 ─────────────────────────────────────────────────

    def new_session_sync(self, op: NewOp) -> SessionInfo:
        """同步新建会话（new 是直接事务、不走 FIFO，无事件循环依赖）。

        供门面在 async 语境下启动任务时同步解析会话（start() 需要句柄一开始
        就持真实 session_id）；常规路径仍走 submit(NewOp)。
        """
        if op.session_id in self._tombstoned:
            raise SessionNotFound(op.session_id)
        return self._new(op)

    async def submit(self, session_id: str, op: SessionOp) -> Any:
        if session_id in self._tombstoned:
            raise SessionNotFound(session_id)
        if not isinstance(op, (NewOp, DeleteOp)) and self._storage.session(session_id) is None:
            # 非 new/delete 的操作都要求会话存在（读原语校验）。
            raise SessionNotFound(session_id)
        if isinstance(op, NewOp):
            # new 无源会话，直接事务，不走 FIFO。
            return await self._do_new(op)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._queues.setdefault(session_id, []).append((op, fut))
        self._ensure_worker(session_id, loop)
        return await fut

    def _ensure_worker(self, sid: str, loop: asyncio.AbstractEventLoop) -> None:
        if sid not in self._workers:
            self._workers[sid] = loop.create_task(self._worker(sid))

    async def _worker(self, sid: str) -> None:
        """处理该会话队列中的操作；异常回给对应 Future，worker 自身不死亡。

        每个 op 的事务失败 / SessionBusy / SessionNotFound 都回给对应
        submit 的 Future，worker 继续下一项；worker 循环自身的未捕获异常
        兜住并重启——队列不允许因异常死亡（否则该会话后续 submit 永久挂起）。
        """
        try:
            while True:
                try:
                    queue = self._queues.get(sid)
                    if not queue:
                        break
                    op, fut = queue.pop(0)
                    if fut.done():
                        continue
                    try:
                        fut.set_result(await self._run_op(op))
                    except BaseException as exc:  # noqa: BLE001 —— 回给调用方
                        fut.set_exception(exc)
                except asyncio.CancelledError:
                    raise
                except BaseException:  # noqa: BLE001
                    # worker 自身异常兜住，短暂让出后继续（不吞掉：下一轮重取）。
                    await asyncio.sleep(0)
        finally:
            self._workers.pop(sid, None)

    async def _run_op(self, op: SessionOp) -> Any:
        if isinstance(op, AppendOp):
            return self._append(op)
        if isinstance(op, SetModelOp):
            return self._set_model(op)
        if isinstance(op, ForkOp):
            return self._fork(op)
        if isinstance(op, ResetOp):
            return self._reset(op)
        if isinstance(op, CompactOp):
            return self._compact(op)
        if isinstance(op, DeleteOp):
            return self._delete(op)
        if isinstance(op, HistoryOp):
            return self._history(op)
        raise TypeError(f"未知操作类型 {type(op).__name__}")

    # ── 动词实现（每个一个 storage.transaction） ──────────────────

    def _sync_op(self, fn: Any, *args: Any) -> Any:
        """storage 是同步 SQLite；在 worker（单线程事件循环）内直接同步执行。

        事务短，设计稿明示「sqlite3 直接在事件循环线程执行」。
        """
        return fn(*args)

    async def _do_new(self, op: NewOp) -> SessionInfo:
        return self._new(op)

    def _new(self, op: NewOp) -> SessionInfo:
        with self._storage.transaction() as conn:
            sid = self._storage.mint_session_id(conn)
            row = SessionRow(
                id=sid,
                bot_id=op.bot_id,
                parent_id=None,
                fork_at=None,
                active_leaf=None,
                model=op.model,
                title=op.title,
                created_at=time.time(),
                origin_parent_id=op.origin_parent_id,
                background=op.background,
                restart_count=op.restart_count,
            )
            self._storage.insert_session(conn, row)
        return self._info(row)

    def _append(self, op: AppendOp) -> str:
        sid = op.session_id
        with self._storage.transaction() as conn:
            s = self._storage.session(sid)
            if s is None:
                raise SessionNotFound(sid)
            # parent 缺省 = 接当前活跃叶；显式传且与活跃叶不一致视为漂移
            # （FIFO 串行下正常不会发生，纯防御）。
            parent = op.parent if op.parent is not None else s.active_leaf
            eid = self._storage.mint_entry_id(conn)
            row = EntryRow(
                id=eid,
                session_id=sid,
                parent_id=parent,
                kind=op.kind,
                content=op.content,
                created_at=time.time(),
            )
            self._storage.insert_entry(conn, row)
            self._storage.update_active_leaf(conn, sid, eid)
        return eid

    def _set_model(self, op: SetModelOp) -> None:
        """会话层模型覆盖：经会话 FIFO 串行写，SQL 只在 storage 层。"""
        with self._storage.transaction() as conn:
            if self._storage.session(op.session_id) is None:
                raise SessionNotFound(op.session_id)
            self._storage.update_session_model(conn, op.session_id, op.model)

    def _fork(self, op: ForkOp) -> SessionInfo:
        sid = op.session_id
        with self._storage.transaction() as conn:
            src = self._storage.session(sid)
            if src is None:
                raise SessionNotFound(sid)
            src_entries = self._storage.entries(sid)
            by_id = {e.id: e for e in src_entries}
            if op.at is not None and op.at not in by_id:
                raise InvalidEntry(op.at)
            # 拷贝前缀 = 从 at（或根）沿 parent 回溯到最老，再时间序回排。
            anchor = op.at if op.at is not None else src.active_leaf
            chain: list[EntryRow] = []
            cur = by_id.get(anchor) if anchor is not None else None
            while cur is not None:
                chain.append(cur)
                cur = by_id.get(cur.parent_id) if cur.parent_id is not None else None
            chain.reverse()
            id_map: dict[str, str] = {}
            new_sid = self._storage.mint_session_id(conn)
            for e in chain:
                neid = self._storage.mint_entry_id(conn)
                id_map[e.id] = neid
            new_leaf: str | None = None
            for e in chain:
                neid = id_map[e.id]
                npid = id_map.get(e.parent_id) if e.parent_id is not None else None
                nrow = EntryRow(
                    id=neid,
                    session_id=new_sid,
                    parent_id=npid,
                    kind=e.kind,
                    content=e.content,
                    created_at=e.created_at,
                )
                self._storage.insert_entry(conn, nrow)
                new_leaf = neid
            new_row = SessionRow(
                id=new_sid,
                bot_id=src.bot_id,
                parent_id=sid,
                fork_at=id_map.get(anchor) if anchor is not None else None,
                active_leaf=new_leaf,
                model=None,
                title=None,
                created_at=time.time(),
                origin_parent_id=None,
                background=False,
                restart_count=0,
            )
            self._storage.insert_session(conn, new_row)
        return self._info(new_row)

    def _reset(self, op: ResetOp) -> str:
        sid = op.session_id
        if self.running_task(sid) is not None:
            raise SessionBusy(sid)
        with self._storage.transaction() as conn:
            s = self._storage.session(sid)
            entries = self._storage.entries(sid)
            by_id = {e.id: e for e in entries}
            to = op.to
            # 祖先校验：to 必须在活跃叶沿 parent 回溯的祖先链上。
            chain_ids = {s.active_leaf} if s.active_leaf is not None else set()
            cur = s.active_leaf
            while cur is not None:
                e = by_id.get(cur)
                if e is None:
                    break
                chain_ids.add(cur)
                cur = e.parent_id
            if to not in by_id or to not in chain_ids:
                raise InvalidEntry(to)
            eid = self._storage.mint_entry_id(conn)
            row = EntryRow(
                id=eid,
                session_id=sid,
                parent_id=to,
                kind="reset",
                content="",
                created_at=time.time(),
            )
            self._storage.insert_entry(conn, row)
            self._storage.update_active_leaf(conn, sid, eid)
        return eid

    def _compact(self, op: CompactOp) -> bool:
        sid = op.session_id
        # 形状守卫：compact 只在任务边界执行；忙碌会话拒绝（与 reset 同语义）。
        # 事务外摘要 + 事务内提交的两段式由 compaction/ 扩展（第 6 步）驱动，
        # 本守卫保证它不会与进行中的任务抢活跃叶。during_task=True 是 loop 在
        # 任务边界内的合法提交路径（FIFO 此刻空闲、无并发 append）。
        if self.running_task(sid) is not None and not op.during_task:
            raise SessionBusy(sid)
        with self._storage.transaction() as conn:
            s = self._storage.session(sid)
            # 最后防线：活跃叶未漂移才允许落摘要；漂移即放弃（返回 False），
            # 扩展层下轮再试。
            if op.leaf is not None and s.active_leaf != op.leaf:
                return False
            eid = self._storage.mint_entry_id(conn)
            row = EntryRow(
                id=eid,
                session_id=sid,
                parent_id=s.active_leaf,
                kind="compaction",
                content=op.summary,
                created_at=time.time(),
            )
            self._storage.insert_entry(conn, row)
            self._storage.update_active_leaf(conn, sid, eid)
        # 提交成功后发 COMPACTION_STARTED（事件流只做活体推送，事实仍以 entry 为准）。
        if self._event_bus is not None:
            self._event_bus.publish(compaction_started_event("", sid, op.summary))
        return True

    def _delete(self, op: DeleteOp) -> None:
        sid = op.session_id
        all_sessions = self._storage.sessions_all()
        tree = _tree_sessions(all_sessions, sid)
        for t in tree:
            if self._running.get(t) is not None or self._queues.get(t):
                raise SessionBusy(t)
        for t in tree:
            self._tombstoned.add(t)
        with self._storage.transaction() as conn:
            self._storage.delete_session_tree(conn, tree)
        for t in tree:
            self._queues.pop(t, None)
            if t in self._workers:
                # worker 会在下一轮发现空队列并退出。
                pass

    def _history(self, op: HistoryOp) -> list[EntryRow]:
        entries = self._storage.entries(op.session_id)
        s = self._storage.session(op.session_id)
        return replay_entries(entries, s.active_leaf if s else None)

    def _info(self, s: SessionRow) -> SessionInfo:
        return SessionInfo(
            id=s.id,
            bot_id=s.bot_id,
            parent_id=s.parent_id,
            active_entry_id=s.active_leaf,
            running_task_id=self._running.get(s.id),
            model=s.model,
            title=s.title,
            created_at=s.created_at,
            background=s.background,
            restart_count=s.restart_count,
        )


@dataclass(frozen=True)
class MessageGroup:
    through: int
    messages: list[dict]


def message_groups(entries: list[HistoryEntry], actions: list[Action]) -> list[MessageGroup]:
    responses: dict[int, list[Action]] = {}
    for action in actions:
        responses.setdefault(action.response_id, []).append(action)
    groups = []
    for entry in entries:
        data = entry.data
        if entry.kind == "input":
            groups.append(MessageGroup(entry.id, [{"role": "user", "content": data["text"]}]))
        elif entry.kind == "assistant":
            calls = responses.get(entry.id, [])
            if any(action.result is None for action in calls):
                continue
            assistant = {"role": "assistant", "content": data["text"]}
            messages = [assistant]
            if calls:
                assistant["tool_calls"] = [model_tool_call(a.call.call_id, a.call.name, a.call.params) for a in calls]
                messages.extend({"role": "tool", "tool_call_id": a.call.call_id, "content": a.result.content} for a in calls)
            groups.append(MessageGroup(entry.id, messages))
        elif entry.kind == "verification_failed":
            messages = [{"role": "user", "content": f"Verification failed. {data['detail']}"}]
            groups.append(MessageGroup(entry.id, messages))
    return groups


def compact_groups(groups: list[MessageGroup], limit: int, force: bool = False) -> tuple[int, str] | None:
    if len(groups) < 3:
        return None
    total = sum(len(group.messages) for group in groups)
    if not force and total <= limit:
        return None
    cut = 0
    while cut < len(groups) - 2 and (total > limit or force and cut == 0):
        total -= len(groups[cut].messages)
        cut += 1
    excerpts = []
    for group in groups[:cut]:
        for message in group.messages:
            content = message["content"].strip().replace("\n", " ")[:180]
            names = ", ".join(call["function"]["name"] for call in message.get("tool_calls", []))
            excerpts.append(f"{message['role']}: {content} {names}".rstrip())
    return groups[cut - 1].through, "\n".join(excerpts)[-2000:]
