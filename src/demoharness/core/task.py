"""当前运行时的 Task、Run、Budget、Action 和验收类型。

Task 代表持续推进的一件事，Run 记录一次执行尝试。TaskStore 保存这些状态。
本文件也保留早期 SDK 使用的 StopReason 和会话树恢复函数。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .plan import Plan
from .tools import ToolCall, ToolResult

if TYPE_CHECKING:
    from .session import SessionManager
    from .storage import EntryRow, Storage

# entry kind 全集（本模块用到的）——与 storage-session.md 的 kind 约定一致。
USER_KIND = "user"
ASSISTANT_KIND = "assistant"
TOOL_CALL_KIND = "tool_call"
TOOL_RESULT_KIND = "tool_result"
COMPACTION_KIND = "compaction"
RESET_KIND = "reset"
INTERRUPTED_KIND = "interrupted"

# stop_reason 元数据在 content JSON 里的键。
STOP_REASON_KEY = "stop_reason"


class StopReason(Enum):
    """Task 终态的权威标记（架构不变量 7，六态）。"""

    COMPLETED = "completed"
    ERROR = "error"
    MAX_ITERATIONS = "max_iterations"
    CANCELLED = "cancelled"
    EMPTY_RESPONSE = "empty_response"
    INTERRUPTED = "interrupted"

    @classmethod
    def of(cls, value: str | StopReason) -> StopReason:
        if isinstance(value, StopReason):
            return value
        return cls(value)


class NextStep(Enum):
    """阶段性下一步：从叶子 kind 推导（单一事实源）。"""

    CALL_MODEL = "call_model"      # 叶子是 tool_result / 无 tool_calls 的 assistant
    EXECUTE_TOOLS = "execute_tools"  # 叶子是 assistant 且带 tool_calls
    DONE = "done"                  # 已终态（terminal entry / interrupted）


@dataclass(frozen=True)
class RecoveryReport:
    session_id: str
    is_background: bool
    phase: str | None
    interrupted_entry_id: str | None

    @property
    def recovered(self) -> bool:
        """本次扫描是否真的物化了中断（false = 已幂等跳过 / 已终态）。"""
        return self.interrupted_entry_id is not None


# ── content 解析（单一事实源：content 是 JSON 序列化 str 或原文） ──


def _parse_content(content: str) -> dict | str:
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return content
    return parsed if isinstance(parsed, dict) else content


def _content_to_str(text: str, metadata: dict | None = None) -> str:
    """把文本 + 可选元数据（如 stop_reason、usage）序列化进 content。

    无元数据时仍返回纯文本 str（保持 entry 内容人可读）；带元数据时返回
    JSON 对象字符串。解析端 _parse_content 对称。
    """
    if not metadata:
        return text
    payload = {"text": text, **metadata}
    return json.dumps(payload, ensure_ascii=False)


# ── phase 推导（纯函数，loop 每轮调用） ──


def decide(entry_prefix: list[EntryRow]) -> NextStep:
    """从活跃分支前缀推导下一步。

    单一事实源（entry）：看叶子 kind——落盘映射以 tools.md「落盘映射」
    为准（tool_call 是独立 entry kind）：
    - 叶子不存在（无任何 entry）→ 该调模型（首轮）
    - 叶子是 user → 该调模型
    - 叶子是 assistant 且带 tool_calls（过渡态）或 tool_call → 该执行工具
    - 叶子是 tool_result → 该调模型（执行完回到模型）
    - 叶子是终态 assistant（content 带 stop_reason）→ DONE
    - 叶子是 interrupted → DONE
    """
    if not entry_prefix:
        return NextStep.CALL_MODEL
    leaf = entry_prefix[-1]
    if leaf.kind == INTERRUPTED_KIND:
        return NextStep.DONE
    if leaf.kind == ASSISTANT_KIND:
        content = _parse_content(leaf.content)
        if isinstance(content, dict) and STOP_REASON_KEY in content:
            return NextStep.DONE
        if isinstance(content, dict) and content.get("tool_calls"):
            return NextStep.EXECUTE_TOOLS
        return NextStep.CALL_MODEL
    if leaf.kind == TOOL_CALL_KIND:
        return NextStep.EXECUTE_TOOLS
    if leaf.kind == TOOL_RESULT_KIND:
        return NextStep.CALL_MODEL
    if leaf.kind == USER_KIND:
        return NextStep.CALL_MODEL
    return NextStep.DONE


# ── stop_reason 落点（方案 A：终态 entry 元数据） ──


def terminal_content(text: str, stop_reason: StopReason) -> str:
    """loop append 终态 assistant entry 时调用——把 stop_reason 写进 content 元数据。"""
    return _content_to_str(text, {STOP_REASON_KEY: stop_reason.value})


def is_terminal_entry(entry: EntryRow) -> bool:
    """终态判定：assistant entry 的 content 元数据带 stop_reason。"""
    if entry.kind != ASSISTANT_KIND:
        return False
    content = _parse_content(entry.content)
    return isinstance(content, dict) and STOP_REASON_KEY in content


def stop_reason_of(entry: EntryRow) -> StopReason | None:
    """从终态 entry 读出 stop_reason 六态（方案 A 的查询侧）。"""
    if not is_terminal_entry(entry):
        return None
    content = _parse_content(entry.content)
    return StopReason.of(content[STOP_REASON_KEY])


def terminal_text(entry: EntryRow) -> str:
    """终态 entry 的文本部分（content 是 JSON 时取 text 字段，否则原文）。"""
    content = _parse_content(entry.content)
    if isinstance(content, dict):
        return content.get("text", "")
    return content


# ── 恢复物化（异常路径与重启共用，恰好一次） ──


async def materialize_interrupted(session_mgr: SessionManager, sid: str) -> str | None:
    """单事务：无结果的 tool_call 物化中断结果 + interrupted entry → active_leaf。

    恰好一次由 interrupted entry 的存在保证——该分支重扫时看到已有
    interrupted kind 即跳过。返回物化出的 interrupted entry id（幂等跳过或
    已终态时返回 None）。loop 的异常 finally 与 BotRuntime 的恢复扫描都调它。

    storage 是同步 SQLite 且事务短（单进程 asyncio 直接在事件循环线程执行，
    见 storage-session.md），故这里直接同步跑核心物化、不额外开线程。
    """
    return _materialize(session_mgr._storage, sid)


def _materialize(storage: Storage, sid: str) -> str | None:
    from .storage import EntryRow

    """物化中断的核心逻辑（async 包装与启动扫描共用同一实现）。

    - 幂等：活跃分支已含 interrupted 或终态 entry → 返回 None。
    - 物化：未配对的 tool_call 各补一条 error tool_result，再落 interrupted
      entry 作为 active_leaf 的终态标记。
    """
    s = storage.session(sid)
    if s is None or s.active_leaf is None:
        return None
    prefix = _replay(storage, sid, s.active_leaf)
    for e in prefix:
        if e.kind == INTERRUPTED_KIND or is_terminal_entry(e):
            return None

    leaf = s.active_leaf
    with storage.transaction() as conn:
        for call_id in _pending_tool_calls(storage.entries(sid), prefix):
            result_row = EntryRow(
                id=storage.mint_entry_id(conn),
                session_id=sid,
                parent_id=leaf,
                kind=TOOL_RESULT_KIND,
                content=json.dumps({"error": "Task interrupted"}),
                created_at=time.time(),
            )
            storage.insert_entry(conn, result_row)
            leaf = result_row.id
        interrupted_row = EntryRow(
            id=storage.mint_entry_id(conn),
            session_id=sid,
            parent_id=leaf,
            kind=INTERRUPTED_KIND,
            content=json.dumps({"phase": "interrupted"}),
            created_at=time.time(),
        )
        storage.insert_entry(conn, interrupted_row)
        storage.update_active_leaf(conn, sid, interrupted_row.id)
        return interrupted_row.id


def _pending_tool_calls(all_entries: list[EntryRow], prefix: list[EntryRow]) -> list[str]:
    """本分支上已落盘但无 tool_result 配对的 tool_call 的 call_id 列表。

    声明为安全的是否重跑不影响本函数——本模块语义是「保守物化」：任何
    已落盘未完成的调用一律物化中断，绝不重执行（架构不变量 5）。
    """
    paired: set[str] = set()
    pending: list[str] = []
    for e in prefix:
        if e.kind == TOOL_CALL_KIND:
            pending.append(_call_id_of(e))
        elif e.kind == TOOL_RESULT_KIND:
            cid = _call_id_of(e)
            if cid:
                paired.add(cid)
    return [c for c in pending if c not in paired]


def _call_id_of(entry: EntryRow) -> str | None:
    """从 tool_call / tool_result 的 content 读 call_id（端到端透传的配对键）。"""
    content = _parse_content(entry.content)
    if isinstance(content, dict):
        return content.get("call_id") or content.get("tool_call_id")
    return None


# ── 恢复扫描（BotRuntime 打开存储时） ──


def scan_and_recover(storage: Storage, session_mgr: SessionManager) -> list[RecoveryReport]:
    """扫出未终态 run 并物化中断，返回报告。

    判定规则（task.md v2）：活跃分支上最后一个 user entry 之后，既无终态
    entry 也无 interrupted entry——即未终态。叶子是什么 kind 不重要（tool_result
    叶子 = 模型等待中断，同样是未终态）。物化逻辑自身幂等，重扫安全。
    """
    reports: list[RecoveryReport] = []
    for srow in storage.sessions_all():
        sid = srow.id
        if srow.active_leaf is None:
            continue
        prefix = _replay(storage, sid, srow.active_leaf)
        if _is_unteminated(prefix):
            interrupted_id = _materialize(storage, sid)
            reports.append(
                RecoveryReport(
                    session_id=sid,
                    is_background=srow.background,
                    phase=_phase_of(prefix),
                    interrupted_entry_id=interrupted_id,
                )
            )
    return reports


def _replay(storage: Storage, sid: str, leaf: str) -> list[EntryRow]:
    from .session import replay_entries

    return replay_entries(storage.entries(sid), leaf)


def _is_unteminated(prefix: list[EntryRow]) -> bool:
    last_user_idx = -1
    for i, e in enumerate(prefix):
        if e.kind == USER_KIND:
            last_user_idx = i
    if last_user_idx == -1:
        return False
    for e in prefix[last_user_idx + 1:]:
        if e.kind == INTERRUPTED_KIND or is_terminal_entry(e):
            return False
    return True


def _phase_of(prefix: list[EntryRow]) -> str | None:
    """把前缀映射成一个可读 phase 快照（供报告与诊断）。"""
    return decide(prefix).value

class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class RunReason(StrEnum):
    COMPLETED = "completed"
    STOPPED = "stopped"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MODEL_ERROR = "model_error"
    MODEL_LENGTH = "model_length"
    MODEL_ABORTED = "model_aborted"
    EMPTY_RESPONSE = "empty_response"
    VERIFICATION_FAILED = "verification_failed"
    UNVERIFIABLE = "unverifiable"
    UNKNOWN_EFFECT = "unknown_effect"
    WORKSPACE_ERROR = "workspace_error"
    RUNTIME_ERROR = "runtime_error"


class Verdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class Budget:
    model_calls: int = 12
    tool_calls: int = 24
    verifications: int = 3
    compactions: int = 3

    def __post_init__(self) -> None:
        for count in vars(self).values():
            if type(count) is not int or count < 0:
                raise ValueError("Budget counts must be non-negative integers")


@dataclass(frozen=True)
class FileCheck:
    path: str
    equals: str | None = None
    contains: str | None = None
    python_syntax: bool = False
    id: str = ""

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("A file check needs a path")
        if not self.id:
            object.__setattr__(self, "id", self.path)


def task_path(path: str) -> str:
    if not isinstance(path, str):
        raise TypeError("A Task path must be a string")
    parts = PurePosixPath(path).parts
    if not parts or any(part.casefold() in {"/", "//", "..", ".git"} for part in parts) or "\0" in path:
        raise ValueError(f"Path outside the file scope: {path!r}")
    return "/".join(parts)


def write_paths(paths: Iterable[str]) -> frozenset[str]:
    if isinstance(paths, (str, bytes)):
        raise TypeError("Write scope must be a collection of file paths")
    return frozenset(task_path(path) for path in paths)


def check_write_paths(checks: Iterable[FileCheck]) -> frozenset[str]:
    paths = set()
    for check in checks:
        try:
            paths.add(task_path(check.path))
        except (TypeError, ValueError):
            continue
    return frozenset(paths)


@dataclass(frozen=True)
class CheckEvidence:
    id: str
    path: str
    verdict: Verdict
    detail: str
    sha256: str | None = None


@dataclass(frozen=True)
class Delivery:
    text: str
    checks: tuple[CheckEvidence, ...]
    diff: str
    change_fingerprint: str | None = None


@dataclass(frozen=True)
class Task:
    id: str
    agent_id: str
    project: Path
    goal: str
    acceptance: tuple[FileCheck, ...]
    workspace: Path
    state: TaskState
    budget: Budget
    capabilities: frozenset[str]
    session_id: str
    base_commit: str | None = None
    workspace_kind: str | None = None
    workspace_ready: bool = False
    reason: RunReason | None = None
    detail: str | None = None
    delivery: Delivery | None = None
    plan: Plan = field(default_factory=Plan)
    allowed_write_paths: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Input:
    id: str
    agent_id: str
    task_id: str
    text: str


@dataclass(frozen=True)
class InputReceipt:
    input_id: str
    task_id: str


@dataclass(frozen=True)
class Run:
    id: str
    task_id: str
    config_version: str
    started_at: float
    ended_at: float | None = None
    reason: RunReason | None = None
    delegation_id: str | None = None


class ActionState(StrEnum):
    PENDING = "pending"
    STARTED = "started"
    RESULT = "result"


@dataclass(frozen=True)
class Action:
    id: str
    task_id: str
    run_id: str
    response_id: int
    position: int
    call: ToolCall
    state: ActionState
    attempts: int
    intent: dict | None
    result: ToolResult | None
