"""One model, tool, and verification loop for Task and delegated runs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .session import compact_groups, message_groups
from .storage import HistoryEntry, TaskStore
from .task import Action, ActionState, CheckEvidence, Delivery, Run, RunReason, TaskState, Verdict
from .tools import RecoverableTools, ToolCall, ToolRejected, ToolResult, ToolSpec, UnknownEffect


class ModelStop(Enum):
    """模型层四态（provider 报给 loop，loop 翻译成任务六态）。"""

    OK = "ok"
    LENGTH = "length"
    ERROR = "error"
    ABORTED = "aborted"


@dataclass(frozen=True)
class ModelRequest:
    system_prompt: str
    messages: list[dict]
    tools: list[ToolSpec]
    model: str
    max_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    stop_reason: ModelStop
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict | None = None
    error: str | None = None


StreamFn = Callable[[ModelRequest, asyncio.Event], "asyncio.Coroutine[Any, Any, ModelResponse]"]


@dataclass(frozen=True)
class RoleResult:
    verdict: Verdict
    detail: str = ""
    checks: tuple[CheckEvidence, ...] = ()
    delivery: Delivery | None = None


@dataclass(frozen=True)
class LoopRole:
    complete: StreamFn
    model: str
    system_prompt: str
    tools: RecoverableTools
    finish: Callable[[str], Awaitable[RoleResult]]
    max_tokens: int | None
    context_messages: int
    context_note: Callable[[], str] | None = None
    model_limit: int | None = None
    after_action: Callable[[Action], None] | None = None


def _calls(response: ModelResponse) -> list[ToolCall]:
    result = []
    ids = set()
    for raw in response.tool_calls:
        call_id, name, params = raw.get("call_id"), raw.get("name"), raw.get("params", {})
        if not isinstance(call_id, str) or not call_id or call_id in ids or not isinstance(name, str) or not isinstance(params, dict):
            raise ValueError("Model returned a malformed or duplicate tool call")
        json.dumps(params, allow_nan=False)
        ids.add(call_id)
        result.append(ToolCall(call_id, name, params))
    return result


def _budget_wait(store: TaskStore, run: Run, resource: str) -> None:
    store.end(run, TaskState.WAITING, RunReason.BUDGET_EXHAUSTED, f"Budget exhausted for {resource}")


async def _advance_action(
    store: TaskStore, run: Run, tools: RecoverableTools, action: Action, cancel: asyncio.Event,
) -> bool:
    try:
        if action.state is ActionState.STARTED:
            result = await tools.reconcile(action)
            if result is not None:
                store.result(run, action, result)
                return True
            if run.delegation_id is None and store.latest_input(run.task_id) > action.response_id:
                store.result(run, action, ToolResult("Skipped retry because the user added new input", is_error=True))
                return True
            tools.authorize(action)
            intent = action.intent
        else:
            intent = await tools.prepare(action)
            if run.delegation_id is None and store.latest_input(run.task_id) > action.response_id:
                store.result(run, action, ToolResult("Skipped because the user added new input", is_error=True))
                return True
        if cancel.is_set():
            return False
        if not store.start_action(run, action, intent):
            _budget_wait(store, run, "tools")
            return False
        result = await tools.execute(action, intent, cancel)
    except UnknownEffect as error:
        store.end(run, TaskState.WAITING, RunReason.UNKNOWN_EFFECT, str(error))
        return False
    except (ToolRejected, OSError, UnicodeError) as error:
        result = ToolResult(str(error), is_error=True)
    store.result(run, action, result)
    return True


def _proposal(entries: list[HistoryEntry]) -> HistoryEntry | None:
    for entry in reversed(entries):
        if entry.kind == "assistant":
            return entry if entry.data["proposal"] else None
        if entry.kind in {"input", "verification_failed", "completed"}:
            break
    return None


async def _verify(store: TaskStore, run: Run, role: LoopRole, proposal: HistoryEntry) -> bool:
    attempt = store.begin(run, "verifications", "verification_started", proposal_id=proposal.id)
    if attempt is None:
        _budget_wait(store, run, "verification")
        return False
    verdict = await role.finish(proposal.data["text"])
    if store.task(run.task_id).state is not TaskState.RUNNING:
        return False
    if run.delegation_id is None and store.latest_input(run.task_id) > proposal.id:
        store.record(run, "verification_superseded", proposal_id=proposal.id, checks=[asdict(c) for c in verdict.checks])
        return True
    if verdict.verdict is Verdict.PASSED:
        store.end(run, TaskState.COMPLETED, RunReason.COMPLETED, delivery=verdict.delivery, checks=verdict.checks)
    elif verdict.verdict is Verdict.UNVERIFIABLE:
        store.end(run, TaskState.WAITING, RunReason.UNVERIFIABLE, verdict.detail, checks=verdict.checks)
    elif not store.task(run.task_id).budget.verifications:
        store.end(run, TaskState.FAILED, RunReason.VERIFICATION_FAILED, verdict.detail, checks=verdict.checks)
    else:
        store.record(run, "verification_failed", detail=verdict.detail, checks=[asdict(x) for x in verdict.checks])
        return True
    return False


def _length_failures(entries: list[HistoryEntry]) -> list[int]:
    failures = []
    for entry in reversed(entries):
        if entry.kind == "model_result":
            if entry.data["stop"] != ModelStop.LENGTH.value:
                break
            failures.append(entry.id)
    return failures


def _context(
    store: TaskStore, run: Run, entries: list[HistoryEntry], actions: list[Action], limit: int, length_id: int | None,
) -> list[dict] | None:
    latest = next((entry.data for entry in reversed(entries) if entry.kind == "summary"), {})
    past = latest.get("text", "")
    groups = message_groups([entry for entry in entries if entry.id > latest.get("through", 0)], actions)
    force = length_id is not None and latest.get("length_id") != length_id
    compacted = compact_groups(groups, limit, force)
    if compacted:
        through, excerpt = compacted
        summary = "\n".join(part for part in (past, excerpt) if part)[-2000:]
        changed = store.begin(run, "compactions", "summary", through=through, text=summary, length_id=length_id)
        if changed:
            past = summary
            groups = [group for group in groups if group.through > through]
        elif force:
            return None
    elif force:
        return None
    messages = [{"role": "user", "content": f"Earlier Session summary:\n{past}"}] if past else []
    messages.extend(message for group in groups for message in group.messages)
    return messages


async def run_loop(store: TaskStore, run: Run, role: LoopRole, cancel: asyncio.Event) -> None:
    while not cancel.is_set() and store.task(run.task_id).state is TaskState.RUNNING:
        actions = store.actions(run.task_id, delegation_id=run.delegation_id)
        action = next((action for action in actions if action.state is not ActionState.RESULT), None)
        if action is not None:
            if (run.delegation_id is None and action.state is ActionState.PENDING
                    and store.latest_input(run.task_id) > action.response_id):
                store.result(run, action, ToolResult("Skipped because the user added new input", is_error=True))
                continue
            if not await _advance_action(store, run, role.tools, action, cancel):
                return
            if role.after_action is not None:
                role.after_action(action)
            continue
        entries = store.history(run.task_id, delegation_id=run.delegation_id)
        proposal = _proposal(entries)
        if proposal is not None:
            if not await _verify(store, run, role, proposal):
                return
            continue
        if role.model_limit is not None and sum(entry.kind == "model_started" for entry in entries) >= role.model_limit:
            _budget_wait(store, run, "delegated model calls")
            return
        lengths = _length_failures(entries)
        if len(lengths) > 1:
            store.end(run, TaskState.FAILED, RunReason.MODEL_LENGTH, "Model exceeded its length limit after one retry")
            return
        messages = _context(store, run, entries, actions, role.context_messages, lengths[0] if lengths else None)
        if messages is None:
            store.end(run, TaskState.FAILED, RunReason.MODEL_LENGTH, "Context cannot be compacted further within budget")
            return
        note = role.context_note() if role.context_note else ""
        instructions = "\n\n".join(filter(None, (role.system_prompt, note)))
        request = ModelRequest(instructions, messages, role.tools.definitions(), role.model, role.max_tokens)
        request_id = store.begin(run, "model_calls", "model_started", model=role.model, max_tokens=role.max_tokens)
        if request_id is None:
            _budget_wait(store, run, "model calls")
            return
        try:
            response = await role.complete(request, cancel)
            calls = _calls(response) if response.stop_reason is ModelStop.OK else []
        except Exception as error:  # noqa: BLE001
            response = ModelResponse(ModelStop.ERROR, "", error=str(error))
            calls = []
        if cancel.is_set() or store.task(run.task_id).state is not TaskState.RUNNING:
            return
        store.model_response(run, request_id, response.stop_reason.value, response.text, calls, response.usage, response.error)
        if run.delegation_id is None and store.latest_input(run.task_id) > request_id:
            continue
        if response.stop_reason is ModelStop.ERROR:
            store.end(run, TaskState.FAILED, RunReason.MODEL_ERROR, response.error or "Model request failed")
            return
        if response.stop_reason is ModelStop.ABORTED:
            store.end(run, TaskState.FAILED, RunReason.MODEL_ABORTED, response.error or "Model aborted the request")
            return
        if response.stop_reason is ModelStop.OK and not (response.text.strip() or calls):
            store.end(run, TaskState.FAILED, RunReason.EMPTY_RESPONSE, "Model returned an empty response")
            return
