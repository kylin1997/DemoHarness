"""当前运行时的 Task、Run、Budget、Action 和验收类型。

Task 代表持续推进的一件事，Run 记录一次执行尝试。TaskStore 保存这些状态。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
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
    REVIEW_REQUIRED = "review_required"
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
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("A file check needs a text path")
        if any(value is not None and not isinstance(value, str) for value in (self.equals, self.contains)):
            raise ValueError("File check content must be text")
        if not isinstance(self.id, str) or type(self.python_syntax) is not bool:
            raise ValueError("A file check needs a text id and a boolean python_syntax")
        if not self.id:
            object.__setattr__(self, "id", self.path)


@dataclass(frozen=True)
class Check:
    id: str
    verifier: str
    params: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (not isinstance(self.id, str) or not self.id.strip() or not isinstance(self.verifier, str)
                or not self.verifier.strip() or not isinstance(self.params, dict)):
            raise ValueError("A Check needs an id, verifier name, and JSON parameters")
        object.__setattr__(self, "params", json.loads(json.dumps(self.params, allow_nan=False)))


@dataclass(frozen=True)
class Verifier:
    name: str
    verify: Callable[[Task, Check, str], tuple[Verdict, str]]


def parse_check(data: dict) -> FileCheck | Check:
    return Check(**data) if "verifier" in data else FileCheck(**data)


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


def check_write_paths(checks: Iterable[FileCheck | Check]) -> frozenset[str]:
    paths = set()
    for check in checks:
        if not isinstance(check, FileCheck):
            continue
        try:
            paths.add(task_path(check.path))
        except (TypeError, ValueError):
            continue
    return frozenset(paths)


@dataclass(frozen=True)
class CheckEvidence:
    id: str
    path: str | None
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
    acceptance: tuple[FileCheck | Check, ...]
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
