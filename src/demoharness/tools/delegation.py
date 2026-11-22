from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from demoharness.core.loop import LoopRole, RoleResult, run_loop
from demoharness.core.storage import HistoryEntry, TaskStore
from demoharness.core.task import Action, Delivery, Run, TaskState, Verdict
from demoharness.core.tools import ToolRejected, ToolResult, ToolSpec, validate_params
from demoharness.tools.files import FileTools


_DELEGATE = ToolSpec(
    "delegate",
    "Ask a temporary read-only subagent to investigate or propose changes. Limit counts its model calls (default 3).",
    {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 4000},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["prompt"],
    },
)
_PARENT_HISTORY = ToolSpec(
    "read_parent_history",
    "Read a page of the parent Task's Session. Pass next_after to continue.",
    {
        "type": "object",
        "properties": {
            "after": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    },
)


def _validate(spec: ToolSpec, action: Action) -> None:
    valid, detail = validate_params(spec, action.call.params)
    if not valid:
        raise ToolRejected(detail)


def _preview(entry: HistoryEntry) -> dict:
    text = json.dumps(entry.data, ensure_ascii=False)
    item = {"id": entry.id, "kind": entry.kind}
    if len(text) > 1000:
        return {**item, "preview": text[:1000], "truncated": True}
    return {**item, "data": entry.data}


async def _finish_child(text: str) -> RoleResult:
    return RoleResult(Verdict.PASSED, delivery=Delivery(text[:1600], (), ""))


class _ChildTools(FileTools):
    def __init__(self, store: TaskStore, task_id: str, files: FileTools):
        super().__init__(files.workspace, files.capabilities & {"read_file"}, ())
        self.store = store
        self.task_id = task_id

    def definitions(self) -> list[ToolSpec]:
        return [*super().definitions(), _PARENT_HISTORY]

    def authorize(self, action: Action) -> None:
        if action.call.name == _PARENT_HISTORY.name:
            _validate(_PARENT_HISTORY, action)
        else:
            super().authorize(action)

    async def reconcile(self, action: Action) -> ToolResult | None:
        if action.call.name == _PARENT_HISTORY.name:
            return None
        return await super().reconcile(action)

    async def execute(self, action: Action, intent: dict | None, cancel: asyncio.Event) -> ToolResult:
        if action.call.name != _PARENT_HISTORY.name:
            return await super().execute(action, intent, cancel)
        after = action.call.params.get("after", 0)
        entries = self.store.history(self.task_id, after, limit=action.call.params.get("limit", 10))
        page = {"entries": [_preview(entry) for entry in entries], "next_after": entries[-1].id if entries else after}
        return ToolResult(json.dumps(page, ensure_ascii=False), {"entry_ids": [entry.id for entry in entries]})


class DelegatingTools:
    def __init__(self, store: TaskStore, parent: Run, role: LoopRole, files: FileTools):
        self.store = store
        self.parent = parent
        self.tools = role.tools
        self.child_role = replace(
            role,
            system_prompt=(
                "Investigate the delegated request using read-only tools. "
                "Read the parent Session if you need background. Return a concise conclusion with evidence."
            ),
            tools=_ChildTools(store, parent.task_id, files),
            context_note=None,
            after_action=None,
            finish=_finish_child,
        )

    def definitions(self) -> list[ToolSpec]:
        return [*self.tools.definitions(), _DELEGATE]

    def authorize(self, action: Action) -> None:
        if action.call.name == _DELEGATE.name:
            _validate(_DELEGATE, action)
            if not action.call.params["prompt"].strip():
                raise ToolRejected("Delegation needs a nonempty prompt")
        else:
            self.tools.authorize(action)

    async def prepare(self, action: Action) -> dict | None:
        if action.call.name == _DELEGATE.name:
            self.authorize(action)
            return None
        return await self.tools.prepare(action)

    async def reconcile(self, action: Action) -> ToolResult | None:
        if action.call.name == _DELEGATE.name:
            return self._result(action)
        return await self.tools.reconcile(action)

    async def execute(self, action: Action, intent: dict | None, cancel: asyncio.Event) -> ToolResult:
        if action.call.name != _DELEGATE.name:
            return await self.tools.execute(action, intent, cancel)
        params = action.call.params
        run = self.store.claim_delegation(self.parent, action, params["prompt"])
        await run_loop(self.store, run, replace(self.child_role, model_limit=params.get("limit", 3)), cancel)
        result = self._result(action)
        if result is None:
            raise asyncio.CancelledError
        return result

    def _result(self, action: Action) -> ToolResult | None:
        outcome = self.store.delegation_outcome(action.task_id, action.id)
        if outcome is None:
            return None
        evidence = []
        for recorded in self.store.actions(action.task_id, delegation_id=action.id):
            if recorded.result is None or recorded.result.is_error:
                continue
            item = {"action_id": recorded.id, "tool": recorded.call.name, **recorded.result.details}
            if recorded.call.name == "read_file":
                item["path"] = recorded.call.params["path"]
            evidence.append(item)
        details = {**outcome, "detail": outcome["detail"][:1000], "delegation_id": action.id, "evidence": evidence[-8:]}
        return ToolResult(json.dumps(details, ensure_ascii=False), details, outcome["status"] != TaskState.COMPLETED)
