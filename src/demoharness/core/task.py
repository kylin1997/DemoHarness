"""当前运行时的 Task、Run、Budget、Action 和验收类型。

Task 代表持续推进的一件事，Run 记录一次执行尝试。TaskStore 保存这些状态。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

from .plan import Plan
from .tools import ToolCall, ToolResult


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
