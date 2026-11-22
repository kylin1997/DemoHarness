from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys

import pytest

from demoharness import AgentDefinition, AgentRuntime, Budget, FileCheck, RunReason, TaskState
from demoharness.core.loop import ModelResponse, ModelStop
from test_agent_runtime import Script, call, done, tools


@pytest.fixture
def project(tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "source.py").write_text("source = 1\n")
    return directory


def test_child_reads_own_parent_on_demand_and_returns_evidence(project, tmp_path):
    delegate_prompt = "Inspect source.py and suggest a finding."
    parent_goal = "PARENT-ONLY context. Write the inspected finding."
    child_trace = "Child-only intermediate reasoning."
    unexpected_calls = [
        call("override", "read_parent_history", task_id="other"),
        call("oversize", "read_parent_history", limit=21),
        call("write", "write_file", path="source.py", content="changed"),
        call("command", "run_command", command="touch forbidden"),
        call("nested", "delegate", prompt="nested"),
        call("check", "run_check", recipe_id="finding.txt"),
        call("plan", "revise_plan"),
    ]
    request_count = []

    async def model(request, cancel):
        request_count.append(request)
        turn = len(request_count)
        if turn == 1:
            return done()
        if turn == 2:
            return tools(call("delegate", "delegate", prompt=delegate_prompt))
        if turn == 3:
            assert request.messages == [{"role": "user", "content": delegate_prompt}]
            assert parent_goal not in request.system_prompt
            assert {spec.name for spec in request.tools} == {"read_file", "read_parent_history"}
            return tools(call("first-page", "read_parent_history", after=0, limit=1), *unexpected_calls)
        if turn == 4:
            first_page = json.loads(next(x["content"] for x in request.messages if x.get("tool_call_id") == "first-page"))
            assert len(first_page["entries"]) == 1
            assert first_page["entries"][0]["data"]["text"] == parent_goal
            return ModelResponse(ModelStop.OK, child_trace, [
                call("next-page", "read_parent_history", after=first_page["next_after"], limit=20),
                call("read", "read_file", path="source.py"),
            ])
        if turn == 5:
            assert request.messages[-1]["content"] == "source = 1\n"
            return done("source.py sets source to 1")
        if turn == 6:
            assert child_trace not in json.dumps(request.messages)
            report = json.loads(request.messages[-1]["content"])
            assert report["status"] == "completed"
            assert report["summary"] == "source.py sets source to 1"
            return tools(call("apply", "write_file", path="finding.txt", content="source is 1"))
        return done()

    async def scenario():
        definition = AgentDefinition("a", project, "scripted", model)
        async with AgentRuntime.open(tmp_path / "state", [definition], observe=False) as runtime:
            agent = runtime.agent("a")
            unrelated = await agent.submit("OTHER-ONLY context", acceptance=[FileCheck("source.py")])
            assert (await agent.wait(unrelated.task_id)).state is TaskState.COMPLETED
            receipt = await agent.submit(
                parent_goal, acceptance=[FileCheck("finding.txt", equals="source is 1")],
                budget=Budget(model_calls=6, tool_calls=5, verifications=2),
            )
            task = await agent.wait(receipt.task_id)
            assert task.state is TaskState.COMPLETED, task.detail
            assert task.budget == Budget(model_calls=0, tool_calls=0, verifications=0)
            assert (task.workspace / "source.py").read_text() == "source = 1\n"
            assert not (task.workspace / "forbidden").exists()
            delegation, applied = agent.actions(task.id)
            parent, child = agent.runs(task.id)
            assert delegation.run_id == parent.id and applied.run_id == parent.id
            assert child.delegation_id == delegation.id and child.task_id == task.id
            assert child.reason is RunReason.COMPLETED
            assert len(agent.notices(task.id)) == 1
            child_history = agent.history(task.id, delegation_id=delegation.id)
            assert all(item.run_id == child.id for item in child_history)
            assert child_trace in json.dumps([item.data for item in child_history])
            assert child_trace not in json.dumps([item.data for item in agent.history(task.id)])
            actions = agent.actions(task.id, delegation_id=delegation.id)
            first, *rejected, next_page, read = actions
            assert len(rejected) == len(unexpected_calls)
            assert all(action.result.is_error and action.attempts == 0 for action in rejected)
            assert "OTHER-ONLY" not in first.result.content + next_page.result.content
            assert child_trace not in next_page.result.content
            assert agent.history(unrelated.task_id, delegation_id=delegation.id) == []
            report = delegation.result.details
            assert report["delegation_id"] == delegation.id
            assert report["evidence"][0]["entry_ids"] == [agent.history(task.id)[0].id]
            assert report["evidence"][-1] == {
                "tool": "read_file", "action_id": read.id, "path": "source.py",
                "sha256": hashlib.sha256(b"source = 1\n").hexdigest(),
            }
        assert len(request_count) == 7

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("models", "tool_calls", "child_limit", "parent_state", "detail"),
    [
        (2, 2, 3, TaskState.WAITING, "Budget exhausted for model calls"),
        (3, 1, 3, TaskState.COMPLETED, "Budget exhausted for tools"),
        (3, 2, 1, TaskState.COMPLETED, "Budget exhausted for delegated model calls"),
    ],
)
def test_child_uses_shared_budget_and_its_own_model_limit(project, tmp_path, models, tool_calls, child_limit, parent_state, detail):
    script = Script(
        tools(call("delegate", "delegate", prompt="Read source", limit=child_limit)),
        tools(call("read", "read_file", path="source.py")),
        done(),
    )

    async def scenario():
        definition = AgentDefinition("a", project, "scripted", script)
        async with AgentRuntime.open(tmp_path / "state", [definition], observe=False) as runtime:
            agent = runtime.agent("a")
            receipt = await agent.submit("Inspect source", acceptance=[FileCheck("source.py")],
                                         budget=Budget(model_calls=models, tool_calls=tool_calls))
            task = await agent.wait(receipt.task_id)
            assert task.state is parent_state, task.detail
            (delegation,) = agent.actions(task.id)
            assert delegation.result.is_error
            assert delegation.result.details["reason"] == RunReason.BUDGET_EXHAUSTED
            assert delegation.result.details["detail"] == detail
            child_events = agent.history(task.id, delegation_id=delegation.id)
            assert sum(item.kind == "model_started" for item in child_events) == 1
            assert sum(item.kind == "action_started" for item in child_events) == tool_calls - 1
            assert len(script.requests) == models
            assert task.budget.model_calls == task.budget.tool_calls == 0

    asyncio.run(scenario())


def test_child_cannot_read_files_without_parent_permission(project, tmp_path):
    script = Script(
        tools(call("delegate", "delegate", prompt="Inspect source")),
        tools(call("read", "read_file", path="source.py")),
        done("No file permission"),
        done(),
    )

    async def scenario():
        definition = AgentDefinition("a", project, "scripted", script, capabilities=frozenset({"run_check"}))
        async with AgentRuntime.open(tmp_path / "state", [definition], observe=False) as runtime:
            agent = runtime.agent("a")
            receipt = await agent.submit("Inspect source", acceptance=[FileCheck("source.py")])
            task = await agent.wait(receipt.task_id)
            assert task.state is TaskState.COMPLETED, task.detail
            assert {spec.name for spec in script.requests[1].tools} == {"read_parent_history"}
            (delegation,) = agent.actions(task.id)
            (read,) = agent.actions(task.id, delegation_id=delegation.id)
            assert read.result.is_error
            assert delegation.result.details["evidence"] == []

    asyncio.run(scenario())


def test_stop_prevents_more_child_calls_and_resume_reuses_delegation(project, tmp_path):
    async def scenario():
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        requests = []
        events = []

        async def model(request, cancel):
            requests.append(request)
            events.append(cancel)
            if len(requests) == 1:
                return tools(call("delegate", "delegate", prompt="Inspect source"))
            if len(requests) == 2:
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    if cancel.is_set():
                        cancelled.set()
            return done()

        definition = AgentDefinition("a", project, "scripted", model)
        async with AgentRuntime.open(tmp_path / "state", [definition], observe=False) as runtime:
            agent = runtime.agent("a")
            receipt = await agent.submit("Inspect source", acceptance=[FileCheck("source.py")],
                                         budget=Budget(model_calls=4, tool_calls=1, verifications=2))
            await asyncio.wait_for(entered.wait(), timeout=2)
            task = await agent.stop(receipt.task_id)
            assert task.state is TaskState.STOPPED
            assert events[0] is events[1] and cancelled.is_set()
            parent, child = agent.runs(task.id)
            assert parent.reason is child.reason is RunReason.STOPPED
            assert len(requests) == 2
            assert [event.kind for event in agent.history(task.id, delegation_id=child.delegation_id)].count("model_interrupted") == 1
            await agent.resume(task.id)
            assert (await agent.wait(task.id)).state is TaskState.COMPLETED
            assert requests[2].messages == requests[1].messages
            assert len(requests) == 4
            second_child = agent.runs(task.id)[-1]
            assert child.id != second_child.id and child.delegation_id == second_child.delegation_id
            (delegation,) = agent.actions(task.id)
            assert delegation.attempts == 1
            assert delegation.result.details["delegation_id"] == child.delegation_id

    asyncio.run(scenario())


_CRASH_AFTER_CHILD_RESULT = """
import asyncio
import os
import signal
import sys
from pathlib import Path

from demoharness import AgentDefinition, AgentRuntime, Budget, FileCheck
from demoharness.core.loop import ModelResponse, ModelStop
from demoharness.core.storage import TaskStore

original_result = TaskStore.result

def interrupt(self, run, action, result):
    if action.call.name == 'delegate':
        os.kill(os.getpid(), signal.SIGKILL)
    original_result(self, run, action, result)

TaskStore.result = interrupt

async def model(request, cancel):
    if 'read_parent_history' in {tool.name for tool in request.tools}:
        return ModelResponse(ModelStop.OK, 'Durable child conclusion')
    return ModelResponse(ModelStop.OK, '', [
        {'call_id': 'delegate', 'name': 'delegate', 'params': {'prompt': 'Inspect the source'}},
    ])

async def run():
    definition = AgentDefinition('a', sys.argv[2], 'scripted', model)
    async with AgentRuntime.open(sys.argv[1], [definition], observe=False) as runtime:
        agent = runtime.agent('a')
        receipt = await agent.submit('Inspect the source', acceptance=[FileCheck('source.py')],
                                     budget=Budget(model_calls=3, tool_calls=1, verifications=2))
        Path(sys.argv[3]).write_text(receipt.task_id)
        await agent.wait(receipt.task_id)

asyncio.run(run())
"""


def test_crash_backfills_completed_child_result_without_replaying(project, tmp_path):
    state = tmp_path / "state"
    receipt_path = tmp_path / "receipt"
    process = subprocess.run(
        [sys.executable, "-c", _CRASH_AFTER_CHILD_RESULT, str(state), str(project), str(receipt_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert process.returncode == -9, process.stderr
    task_id = receipt_path.read_text()
    script = Script(done())

    async def scenario():
        definition = AgentDefinition("a", project, "scripted", script)
        async with AgentRuntime.open(state, [definition], observe=False) as runtime:
            agent = runtime.agent("a")
            task = await agent.wait(task_id)
            assert task.state is TaskState.COMPLETED, task.detail
            interrupted, child, recovered = agent.runs(task.id)
            assert interrupted.reason is RunReason.INTERRUPTED
            assert child.reason is recovered.reason is RunReason.COMPLETED
            (delegation,) = agent.actions(task.id)
            assert child.delegation_id == delegation.id and delegation.attempts == 1
            assert task.budget.tool_calls == 0
            assert delegation.result.details["summary"] == "Durable child conclusion"
            child_history = agent.history(task.id, delegation_id=delegation.id)
            assert [item.kind for item in child_history].count("delegation_finished") == 1
        assert len(script.requests) == 1
        assert "Durable child conclusion" in script.requests[0].messages[-1]["content"]

    asyncio.run(scenario())
