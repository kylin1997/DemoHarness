"""Plan updates run through the same tool execution boundary as file tools."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from functools import partial

from ..core.plan import Plan, StepState
from ..core.storage import TaskStore
from ..core.task import Action, Run
from ..core.tools import (
    ReplayPolicy,
    ToolRejected,
    ToolResult,
    ToolSpec,
    validate_params,
)
from .files import FileTools, Workspace

_TEXT = {"type": "string"}
_REVISION = {"type": "integer", "minimum": 0}
_PLAN_SPECS = {
    "set_plan": ToolSpec("set_plan", "Revise active steps at the current Plan revision. Omitted steps are retired.", {
        "type": "object",
        "properties": {"steps": {"type": "array", "items": _TEXT}, "reason": _TEXT, "expected_revision": _REVISION},
        "required": ["steps", "reason", "expected_revision"],
    }, replay=ReplayPolicy.SAFE),
    "mark_step": ToolSpec("mark_step", "Update an existing Plan step with its progress or evidence.", {
        "type": "object",
        "properties": {
            "step_id": _TEXT,
            "state": {"type": "string", "enum": [state.value for state in StepState if state is not StepState.RETIRED]},
            "evidence": {"type": "array", "items": _TEXT},
            "expected_revision": _REVISION,
        },
        "required": ["step_id", "state", "evidence", "expected_revision"],
    }, replay=ReplayPolicy.SAFE),
}


class PlanningTools(FileTools):
    def __init__(self, workspace: Workspace, capabilities, acceptance, store: TaskStore, run: Run):
        super().__init__(workspace, capabilities, acceptance)
        self.store = store
        self.run = run

    def definitions(self) -> list[ToolSpec]:
        return [*super().definitions(), *_PLAN_SPECS.values()]

    def allowed_write_paths(self) -> frozenset[str]:
        return self.store.task(self.run.task_id).allowed_write_paths

    def authorize(self, action: Action) -> None:
        spec = _PLAN_SPECS.get(action.call.name)
        if spec is None:
            super().authorize(action)
            return
        valid, reason = validate_params(spec, action.call.params)
        if not valid:
            raise ToolRejected(reason)

    async def reconcile(self, action: Action) -> ToolResult | None:
        if action.call.name in _PLAN_SPECS:
            saved = self.store.plan_for_action(self.run.task_id, action.id)
            return ToolResult(json.dumps(asdict(saved), ensure_ascii=False)) if saved is not None else None
        return await super().reconcile(action)

    async def execute(self, action: Action, intent: dict | None, cancel: asyncio.Event) -> ToolResult:
        name, params = action.call.name, action.call.params
        if name not in _PLAN_SPECS:
            return await super().execute(action, intent, cancel)
        try:
            if name == "set_plan":
                reason = params["reason"].strip()
                if not reason:
                    raise ValueError("计划变更需要说明原因")
                update = partial(Plan.revise, actions=params["steps"])
            else:
                reason = "步骤状态更新"
                update = partial(Plan.mark, step_id=params["step_id"], state=StepState(params["state"]),
                                 evidence=tuple(params["evidence"]))
            updated = self.store.set_plan(self.run, params["expected_revision"], update, reason, action.id)
        except ValueError as error:
            raise ToolRejected(str(error)) from error
        return ToolResult(json.dumps(asdict(updated), ensure_ascii=False))
