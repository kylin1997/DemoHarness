import asyncio
from dataclasses import replace

import pytest

from demoharness import (
    AgentDefinition,
    AgentRuntime,
    Budget,
    Check,
    RegisteredTool,
    ReplayPolicy,
    RunReason,
    TaskState,
    ToolResult,
    ToolSpec,
    Verdict,
    Verifier,
)
from demoharness.core.loop import ModelResponse, ModelStop
from demoharness.core.storage import TaskStore


@pytest.mark.parametrize("policy", [ReplayPolicy.NEVER, ReplayPolicy.SAFE])
def test_registered_tool_recovery_respects_original_replay_policy(tmp_path, monkeypatch, policy):
    project = tmp_path / "project"
    project.mkdir()
    executions = []

    def record(params):
        executions.append(params)
        return ToolResult("recorded")

    async def model(request, cancel):
        if any(message["role"] == "tool" for message in request.messages):
            return ModelResponse(ModelStop.OK, "done")
        return ModelResponse(ModelStop.OK, "", [{"call_id": "call", "name": "record", "params": {}}])

    def interrupt(*args):
        raise asyncio.CancelledError

    async def exercise():
        spec = ToolSpec("record", "Record a request", {}, replay=policy)
        definition = AgentDefinition("a", project, "scripted", model, tools=(RegisteredTool(spec, record),))
        state = tmp_path / "state"
        with monkeypatch.context() as patch:
            patch.setattr(TaskStore, "result", interrupt)
            async with AgentRuntime.open(state, [definition], observe=False) as runtime:
                agent = runtime.agent("a")
                task_id = (await agent.submit("Record a request")).task_id
                assert (await agent.wait(task_id)).state is TaskState.QUEUED
        assert len(executions) == 1
        new_tool = RegisteredTool(replace(spec, replay=ReplayPolicy.SAFE), record)
        async with AgentRuntime.open(state, [replace(definition, tools=(new_tool,))], observe=False) as runtime:
            task = await runtime.agent("a").wait(task_id)
            if policy is ReplayPolicy.SAFE:
                assert task.reason is RunReason.REVIEW_REQUIRED and len(executions) == 2
            else:
                assert task.reason is RunReason.UNKNOWN_EFFECT and len(executions) == 1

    asyncio.run(exercise())


def test_removed_verifier_waits_with_evidence(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    async def model(request, cancel):
        return ModelResponse(ModelStop.OK, "42")

    async def exercise():
        definition = AgentDefinition("a", project, "scripted", model,
                                     verifiers=(Verifier("answer", lambda *_: (Verdict.PASSED, "ok")),))
        state = tmp_path / "state"
        async with AgentRuntime.open(state, [definition], observe=False) as runtime:
            agent = runtime.agent("a")
            task_id = (await agent.submit("Answer", acceptance=[Check("total", "answer")],
                                          budget=Budget(model_calls=0))).task_id
            assert (await agent.wait(task_id)).reason is RunReason.BUDGET_EXHAUSTED
        async with AgentRuntime.open(state, [replace(definition, verifiers=())], observe=False) as runtime:
            agent = runtime.agent("a")
            await agent.resume(task_id, budget=Budget(model_calls=1))
            task = await agent.wait(task_id)
            assert task.reason is RunReason.UNVERIFIABLE and task.delivery is None
            assert "Missing verifier: answer" in task.detail

    asyncio.run(exercise())
