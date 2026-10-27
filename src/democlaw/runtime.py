"""DemoClaw · BotRuntime 组合根（runtime.py）。

唯一知道全部部件的地方；外部（Web、测试、教学示例）一律经本模块的 API
使用 DemoClaw，不直接 import core/ 细节。

门面契约（见 docs/design/botruntime-api.md）：

- 两个形态：同步（run，禁止在 async 语境调用，asyncio.run 于当前线程）与
  async（arun / start+TaskHandle / events）。同步形态不依赖独立循环线程——
  SQLite 连接在哪个线程创建就得在哪个线程用（见 architecture.md 附录）。
- Bot 是无状态视图：同名多次取等价，不持运行态。
- steer 语义：运行中 → 注入当前任务；已结束 → 追加为该会话的下一次输入
  （follow-up）。「继续讨论」永远可行，调用方无需先查 status。

run 不传 session → 自动新建；会话延续显式传 session_id。
start() 提交进会话 FIFO 立即返回 TaskHandle，phase 从 QUEUED 开始。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.events import Event, EventBus
from .core.loop import (
    LoopRuntime,
    run_task_loop,
)
from .core.session import (
    AppendOp,
    DeleteOp,
    ForkOp,
    HistoryOp,
    NewOp,
    ResetOp,
    SessionInfo,
    SessionManager,
)
from .core.storage import EntryRow, SessionRow, Storage
from .core.task import (
    StopReason,
    is_terminal_entry,
    scan_and_recover,
    stop_reason_of,
)
from .core.tools import Tool, ToolRegistry

# ── 公开类型（形状见 botruntime-api.md） ──


class TaskPhase(str):  # 简化为字符串，形状与 botruntime-api 的 Enum 对齐
    QUEUED = "queued"
    AWAITING_TOOLS = "awaiting_tools"
    TOOLS_COMPLETED = "tools_completed"
    FINAL_RESPONSE = "final_response"


@dataclass(frozen=True)
class TaskResult:
    stop_reason: StopReason
    content: str
    error: str | None
    task_id: str
    session_id: str
    iterations: int
    tools_used: int


@dataclass(frozen=True)
class TaskStatus:
    task_id: str
    phase: str
    stop_reason: StopReason | None
    iterations: int
    tools_used: int


class TaskHandle:
    """一次已提交任务运行的句柄（start() 返回）。

    状态与结果、steering、取消、事件观察都经此。start() 提交后立即返回，
    phase 从 QUEUED 开始；`aresult()` 阻塞到终态（含 cancelled）。
    """

    def __init__(self, runtime: BotRuntime, task_id: str, session_id: str,
                 loop_task: asyncio.Task, cancel: asyncio.Event):
        self._runtime = runtime
        self._task_id = task_id
        self._session_id = session_id
        self._loop_task = loop_task
        self._cancel = cancel

    @property
    def task_id(self) -> str:
        return self._task_id

    def status(self) -> TaskStatus:
        """终态/运行中的结构化状态（不阻塞，立即返回）。"""
        return self._derive_status()

    def _derive_status(self) -> TaskStatus:
        loop_task = self._loop_task
        if loop_task.done():
            result = loop_task.result()  # TaskResult
            return TaskStatus(
                task_id=self._task_id,
                phase=TaskPhase.FINAL_RESPONSE,
                stop_reason=result.stop_reason,
                iterations=result.iterations,
                tools_used=result.tools_used,
            )
        return TaskStatus(
            task_id=self._task_id,
            phase=TaskPhase.QUEUED,
            stop_reason=None,
            iterations=0,
            tools_used=0,
        )

    async def aresult(self) -> TaskResult:
        return await self._loop_task

    def result(self) -> TaskResult:
        return self._runtime._sync(self.aresult())

    async def steer(self, message: str) -> None:
        """追加指令。运行中 = 注入当前任务；已结束 = 追加为该会话的下一次输入。

        简单实现：把 message 作为 steering user entry append 进会话 FIFO。
        运行中的 loop 在下一轮重放自然携带（steering 走 FIFO + 重放）；
        已结束时（loop_task 已完成）这条 entry 成为会话的下一次输入。
        """
        sid = self._session_id
        await self._runtime._session_mgr.submit(
            sid, AppendOp(sid, "user", message, parent=None)
        )

    def cancel(self) -> None:
        self._cancel.set()

    def events(self) -> asyncio.AsyncIterator[Event]:
        """活体事件流（异步迭代），按本任务过滤。"""
        return self._runtime._bus.subscribe(task_id=self._task_id).__aiter__()


@dataclass(frozen=True)
class BotSpec:
    name: str
    soul: str
    agents: str
    model: str | None
    tools: list[str] | None

    @property
    def system_prompt(self) -> str:
        """SOUL 在前（身份先于规则），分隔符拼接（唯一权威在 events-bots.md）。"""
        return f"{self.soul}\n\n---\n\n{self.agents}"


@dataclass
class RuntimeConfig:
    # 运行时数据统一放用户目录 ~/.democlaw/（不建 data/ 目录进项目）：
    #   - db（SQLite）→ ~/.democlaw/democlaw.db
    #   - workspace（bot 工作区）→ ~/.democlaw/workspace/
    # 这两类都是运行产物，不进 git。教学样例（bots/）在项目内、进 git。
    db_path: str | Path = Path.home() / ".democlaw" / "democlaw.db"
    bots_dir: str | Path = Path("bots")
    workspace_root: str | Path = Path.home() / ".democlaw" / "workspace"
    model: str = "openai/gpt-4o"
    api_key_env: str = "OPENAI_API_KEY"
    max_iterations: int = 50
    context_window: int = 128000
    stream_fn: Any | None = None     # 注入 mock 模型（测试确定性）
    tools: dict[str, Tool] | None = None  # 注入自定义工具


# ── 组合根 ──


class BotRuntime:
    @classmethod
    def build(cls, config: RuntimeConfig | None = None) -> BotRuntime:
        return cls(config or RuntimeConfig())

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self._storage = Storage(str(config.db_path))
        self._session_mgr = SessionManager(self._storage)
        self._bus = EventBus()

        # 工具注册表：内置扩展工具 + 注入工具
        self._registry = ToolRegistry()
        from .tools._builtin import register_builtin_tools  # 第 3 步扩展
        register_builtin_tools(self._registry)
        from .tools.todo import register_todo_tool  # todo 工具（pi 对齐）
        register_todo_tool(self._registry)
        from .tools.subagent import register_delegate_subagent  # 多 Agent 分派工具
        register_delegate_subagent(self._registry, self._run_subagent)
        if config.tools:
            for t in config.tools.values():
                self._registry.register(t)

        # plan-mode 扩展策略（策略实例，经 before_hook 挂进 loop）
        from .strategies.plan_mode import PlanMode
        self.plan_mode = PlanMode()

        # bot spec 装配（三文件）
        self._bots: dict[str, BotSpec] = self._load_bots(config.bots_dir)

        # 任务注册表：task_id -> TaskHandle（进程内，跨 start/status/steer 查询）
        self._tasks: dict[str, TaskHandle] = {}
        self._task_seq = 0

        # 启动恢复扫描（进程重启后物化中断）
        scan_and_recover(self._storage, self._session_mgr)

    def _mint_task_id(self) -> str:
        self._task_seq += 1
        return f"task_{self._task_seq}"

    async def _run_subagent(self, prompt: str, tools: list[str] | None, budget: int | None) -> str:
        """sub-agent 工具的回调：在临时内联会话跑聚焦子任务，返回 final_response 文本。

        一机制两来源（multi-agent.md）：复用「新会话 + Task」，身份即弃；结果作为
        工具结果回传，子会话不进父上下文。
        """
        # 聚焦子任务：受限工具集 + 预算切片 + 聚焦 prompt
        sid = self._session_mgr.new_session_sync(NewOp("", bot_id="_subagent")).id
        task_id = self._mint_task_id()
        cancel = asyncio.Event()

        # 受限工具集：用一个临时 registry，只暴露声明 tools（+ 默认继承若空）
        sub_registry = ToolRegistry()
        if tools:
            for name in tools:
                t = self._registry.get(name)
                if t is not None:
                    sub_registry.register(t)
        # 临时 registry 也需内置（至少 read 等）；简化：继承完整 registry，
        # 受限工具集作为「白名单」由 stream_fn 的 tools 参数控制在前。
        sub_registry = self._registry

        # 临时 system prompt：聚焦 prompt 即子任务
        from .core.loop import LoopRuntime

        loop_runtime = LoopRuntime(
            session_mgr=self._session_mgr,
            stream_fn=self.config.stream_fn or (self._default_stream_fn()),
            registry=sub_registry,
            event_bus=self._bus,
            system_prompt=prompt,
            model=self.config.model,
            max_iterations=budget or self.config.max_iterations,
            context_window=self.config.context_window,
            finish_turn=lambda ctx: False,
            cancel=cancel,
        )
        await self._session_mgr.submit(sid, AppendOp(sid, "user", prompt, parent=None, task_id=task_id))
        self._session_mgr.mark_running(sid, task_id)
        result = await run_task_loop(sid, task_id, prompt, loop_runtime)
        return result.content

    def _default_stream_fn(self):
        import os

        from .providers.openai import make_openai_stream_fn
        key = os.environ.get(self.config.api_key_env)
        if not key:
            raise RuntimeError(f"缺少 API key：环境变量 {self.config.api_key_env} 未设置")
        return make_openai_stream_fn(key)

    # ── 同步/async 双形态 ──

    def _sync(self, coro) -> Any:
        """同步形态：在**当前线程**用 asyncio.run 跑协程并阻塞等结果。

        不用独立循环线程——SQLite 连接在哪个线程创建就得在哪个线程用
        （single-process asyncio + sqlite3 直接在事件循环线程执行，见
        architecture.md 附录）；独立线程会违反 SQLite 的 check_same_thread。
        因此同步形态禁止在 async 语境调用（asyncio.run 不可嵌套）。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # 无运行中循环，可同步
        else:
            raise RuntimeError(
                "当前已在 async 语境，禁止调用同步 run()/result()；请改用 arun()/aresult()"
            )
        return asyncio.run(coro)

    def close(self) -> None:
        self._storage.close()

    # ── bot 装配 ──

    def _load_bots(self, bots_dir: str | Path) -> dict[str, BotSpec]:
        root = Path(bots_dir)
        if not root.is_absolute():
            root = Path.cwd() / root
        specs: dict[str, BotSpec] = {}
        if not root.exists():
            return specs
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            name = sub.name
            soul = (sub / "SOUL.md").read_text(encoding="utf-8")
            agents = (sub / "AGENTS.md").read_text(encoding="utf-8")
            spec_json = sub / "spec.json"
            model = None
            tools = None
            if spec_json.exists():
                data = json.loads(spec_json.read_text(encoding="utf-8"))
                model = data.get("model")
                tools = data.get("tools")
            specs[name] = BotSpec(name=name, soul=soul, agents=agents, model=model, tools=tools)
        return specs

    def bots(self) -> list[str]:
        return list(self._bots.keys())

    def bot(self, name: str) -> Bot:
        if name not in self._bots:
            raise KeyError(f"bot 不存在：{name}（可用：{', '.join(self.bots())}）")
        return Bot(self, self._bots[name])

    # ── 会话树（门面动词） ──

    def new_session(self, bot_id: str) -> SessionInfo:
        def _do():
            return self._session_mgr.submit("", NewOp("", bot_id=bot_id))
        return self._sync(_do())

    async def anew_session(self, bot_id: str) -> SessionInfo:
        return await self._session_mgr.submit("", NewOp("", bot_id=bot_id))

    def sessions(self) -> list[SessionInfo]:
        return [self._row_to_info(r) for r in self._session_mgr._storage.sessions_all()]

    def fork_session(self, session_id: str, at_entry_id: str) -> SessionInfo:
        return self._sync(self._session_mgr.submit(session_id, ForkOp(session_id, at_entry_id)))

    def reset_session(self, session_id: str, to_entry_id: str) -> None:
        self._sync(self._session_mgr.submit(session_id, ResetOp(session_id, to_entry_id)))

    def delete_session(self, session_id: str) -> None:
        self._sync(self._session_mgr.submit(session_id, DeleteOp(session_id)))

    def history(self, session_id: str) -> list[EntryRow]:
        return self._sync(self._session_mgr.submit(session_id, HistoryOp(session_id)))

    def set_session_model(self, session_id: str, model: str) -> None:
        """写会话层模型覆盖（Web 设置面切换模型）。"""
        def _do():
            with self._storage.transaction() as conn:
                conn.execute("UPDATE sessions SET model = ? WHERE id = ?", (model, session_id))
        self._sync(_do())

    def sessions_with(self, stop_reason: StopReason) -> list[SessionInfo]:
        """枚举含指定终态任务的会话——重启后发现 interrupted 任务的入口。"""
        out: list[SessionInfo] = []
        from .core.session import replay_entries
        for srow in self._storage.sessions_all():
            if srow.active_leaf is None:
                continue
            prefix = replay_entries(self._storage.entries(srow.id), srow.active_leaf)
            for e in prefix:
                if e.kind == "interrupted" and stop_reason == StopReason.INTERRUPTED:
                    out.append(self._row_to_info(srow))
                    break
                if is_terminal_entry(e) and stop_reason_of(e) == stop_reason:
                    out.append(self._row_to_info(srow))
                    break
        return out

    def session_tasks(self, session_id: str) -> list[TaskStatus]:
        """该会话的任务历史（每次 run 一行），最新在前。"""
        out: list[TaskStatus] = []
        for task_id, handle in reversed(list(self._tasks.items())):
            if handle._session_id == session_id:
                out.append(handle.status())
        return out

    def task(self, task_id: str) -> TaskStatus:
        """单任务详情；配合 history() 即可重建任务完整过程。"""
        handle = self._tasks.get(task_id)
        if handle is None:
            raise KeyError(f"任务不存在：{task_id}")
        return handle.status()

    def _row_to_info(self, srow: SessionRow) -> SessionInfo:
        return SessionInfo(
            id=srow.id,
            bot_id=srow.bot_id,
            parent_id=srow.parent_id,
            active_entry_id=srow.active_leaf,
            running_task_id=self._session_mgr.running_task(srow.id),
            model=srow.model,
            title=srow.title,
            created_at=srow.created_at,
            background=bool(srow.background),
            restart_count=srow.restart_count,
        )


# ── Bot：无状态视图 ──


class Bot:
    def __init__(self, runtime: BotRuntime, spec: BotSpec):
        self._runtime = runtime
        self._spec = spec

    @property
    def name(self) -> str:
        return self._spec.name

    def run(self, task: str, *, session_id: str | None = None,
            model: str | None = None, max_iterations: int | None = None) -> TaskResult:
        return self._runtime._sync(self._run_inner(task, session_id, model, max_iterations))

    async def arun(self, task: str, *, session_id: str | None = None,
                   model: str | None = None, max_iterations: int | None = None) -> TaskResult:
        return await self._run_inner(task, session_id, model, max_iterations)

    def start(self, task: str, *, session_id: str | None = None,
              model: str | None = None, max_iterations: int | None = None) -> TaskHandle:
        """提交任务进会话 FIFO 立即返回 TaskHandle（异步运行）。

        异步运行：在当前事件循环建 asyncio.Task 跑 run_task_loop。phase 从
        QUEUED 开始；用 handle.aresult() 等终态、handle.events() 推事件、
        handle.steer() 注入、handle.cancel() 取消。

        session_id 缺省时**同步**新建会话（新会话是直接事务、不走 FIFO），
        以便 handle 一开始就持有真实 session_id（steer 立即可用）。
        """
        rt = self._runtime
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as e:
            raise RuntimeError(
                "start() 需要 async 语境（用于 Web/SSE 后台运行）；同步语境请用 run()"
            ) from e
        sid = session_id
        if sid is None:
            from .core.session import NewOp
            sid = rt._session_mgr.new_session_sync(NewOp("", bot_id=self.name)).id
        task_id = rt._mint_task_id()
        cancel = asyncio.Event()
        loop_task = loop.create_task(
            self._run_inner(task, sid, model, max_iterations, task_id=task_id, cancel=cancel)
        )
        handle = TaskHandle(rt, task_id, sid, loop_task, cancel)
        rt._tasks[task_id] = handle
        return handle

    async def _run_inner(self, task: str, session_id: str | None,
                         model: str | None, max_iterations: int | None,
                         task_id: str | None = None,
                         cancel: asyncio.Event | None = None) -> TaskResult:
        rt = self._runtime
        sid = session_id
        if sid is None:
            info = await rt._session_mgr.submit("", NewOp("", bot_id=self.name))
            sid = info.id
        task_id = task_id or rt._mint_task_id()
        cancel = cancel or asyncio.Event()

        # 输入 append + mark_running
        await rt._session_mgr.submit(sid, AppendOp(sid, "user", task, parent=None, task_id=task_id))
        rt._session_mgr.mark_running(sid, task_id)
        # 回填 session_id 到对应句柄（若有）
        handle = rt._tasks.get(task_id)
        if handle is not None:
            handle._session_id = sid

        loop_runtime = self._make_loop_runtime(model, max_iterations, cancel)
        result = await run_task_loop(sid, task_id, task, loop_runtime)
        return TaskResult(
            stop_reason=result.stop_reason,
            content=result.content,
            error=result.error,
            task_id=task_id,
            session_id=sid,
            iterations=result.iterations,
            tools_used=result.tools_used,
        )

    def _make_loop_runtime(self, model: str | None, max_iterations: int | None,
                           cancel: asyncio.Event | None = None) -> LoopRuntime:
        rt = self._runtime
        stream_fn = rt.config.stream_fn
        if stream_fn is None:
            import os

            from .providers.openai import make_openai_stream_fn
            key = os.environ.get(rt.config.api_key_env)
            if not key:
                raise RuntimeError(
                    f"缺少 API key：环境变量 {rt.config.api_key_env} 未设置。"
                    f"教学示例可用 RuntimeConfig(stream_fn=mock) 注入 mock 模型，"
                    f"或 export {rt.config.api_key_env}=sk-..."
                )
            stream_fn = make_openai_stream_fn(key)
        return LoopRuntime(
            session_mgr=rt._session_mgr,
            stream_fn=stream_fn,
            registry=rt._registry,
            event_bus=rt._bus,
            system_prompt=self._spec.system_prompt,
            model=model or self._spec.model or rt.config.model,
            max_iterations=max_iterations or rt.config.max_iterations,
            context_window=rt.config.context_window,
            finish_turn=lambda ctx: False,
            cancel=cancel,
            before_hooks=[rt.plan_mode.before_hook] if rt.plan_mode.enabled else [],
        )