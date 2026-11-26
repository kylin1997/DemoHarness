from __future__ import annotations

import json
from pathlib import Path

from .core.loop import ModelRequest, ModelResponse, ModelStop
from .core.task import Check, Task, Verdict, Verifier
from .core.tools import RegisteredTool, ReplayPolicy, ToolResult, ToolSpec
from .runtime import AgentDefinition

BRIEF_REQUEST = "整理上线简报"
CALENDAR_UPDATE = "上线时间改为周五"
SUM_REQUEST = "计算 12 + 30"
_BRIEF_STEPS = ["整理会议决定", "提交上线简报"]
_UPDATED_STEPS = ["整理会议决定", "核对日历更新", "提交上线简报"]


def demo_agent(agent_id: str, project: Path) -> AgentDefinition:
    return AgentDefinition(
        agent_id,
        project,
        "scripted",
        _complete,
        tools=(RegisteredTool(ToolSpec(
            "add",
            "Add two integers.",
            {
                "type": "object",
                "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
                "required": ["left", "right"],
            },
            replay=ReplayPolicy.SAFE,
        ), _add),),
        verifiers=(Verifier("exact_text", _exact_text),),
    )


def _add(params: dict) -> ToolResult:
    return ToolResult(str(params["left"] + params["right"]))


def _exact_text(task: Task, check: Check, text: str) -> tuple[Verdict, str]:
    if text.strip() == check.params["expected"]:
        return Verdict.PASSED, "交付文本与预期值一致"
    return Verdict.FAILED, "交付文本与预期值不符"


def _route(data: dict) -> dict:
    if data["kind"] == "event":
        if CALENDAR_UPDATE not in data["text"]:
            return {"kind": "skip"}
        for task in data["tasks"]:
            if task["goal"] == BRIEF_REQUEST:
                return {"kind": "attach", "task_id": task["id"]}
        return {"kind": "create", "text": BRIEF_REQUEST}
    if data["text"].strip() == BRIEF_REQUEST:
        return {"kind": "create", "text": BRIEF_REQUEST}
    if data["text"].strip() == SUM_REQUEST:
        return {
            "kind": "create",
            "text": SUM_REQUEST,
            "checks": [{"id": "sum", "verifier": "exact_text", "params": {"expected": "42"}}],
        }
    return {"kind": "reply", "text": f"教学模式可输入「{BRIEF_REQUEST}」或「{SUM_REQUEST}」。"}


async def _complete(request: ModelRequest, cancel) -> ModelResponse:
    if not request.tools:
        data = json.loads(request.messages[-1]["content"])
        return ModelResponse(ModelStop.OK, json.dumps(_route(data), ensure_ascii=False))

    note = request.system_prompt.splitlines()
    results = [message["content"] for message in request.messages if message["role"] == "tool"]
    if f"Current Task goal: {SUM_REQUEST}" in note:
        if results:
            return ModelResponse(ModelStop.OK, results[-1])
        return ModelResponse(ModelStop.OK, "", [{
            "call_id": "sum",
            "name": "add",
            "params": {"left": 12, "right": 30},
        }])

    updated = any(
        CALENDAR_UPDATE in message["content"] for message in request.messages if message["role"] == "user"
    )
    steps = _UPDATED_STEPS if updated else _BRIEF_STEPS
    plan = json.loads(next(line.removeprefix("Current Plan: ") for line in note if line.startswith("Current Plan: ")))
    active_steps = [step["action"] for step in plan["steps"] if step["state"] != "retired"]
    if active_steps != steps:
        return ModelResponse(ModelStop.OK, "", [{
            "call_id": f"plan-{plan['revision']}",
            "name": "set_plan",
            "params": {"steps": steps, "reason": "收到日历更新" if updated else "分解任务",
                       "expected_revision": plan["revision"]},
        }])
    day = "周五" if updated else "周三"
    return ModelResponse(ModelStop.OK, f"上线简报\n\n计划于{day}上线，交付组负责。")
