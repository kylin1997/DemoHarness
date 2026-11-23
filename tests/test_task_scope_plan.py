import asyncio
import json
import sqlite3
import subprocess

import pytest

from demoharness import (
    AgentDefinition,
    AgentRuntime,
    Budget,
    FileCheck,
    RunReason,
    TaskState,
)
from demoharness.core.loop import ModelResponse, ModelStop
from demoharness.core.plan import StepState


def tool(call_id, name, **params):
    return {"call_id": call_id, "name": name, "params": params}


def reply(*tools, text=""):
    return ModelResponse(ModelStop.OK, text, list(tools))


def write(call_id, path, content):
    return tool(call_id, "write_file", path=path, content=content)


def test_only_a_typed_scope_change_authorizes_an_extra_path(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "NOTES.md").write_text("original\n")
    responses = iter([
        reply(write("readme", "./README.md", "ready\n"), write("extra", "NOTES.md", "extra\n")),
        reply(text="README ready"),
        reply(write("still-extra", "NOTES.md", "extra\n")),
        reply(text="Existing notes checked"),
        reply(write("confirmed", "NOTES.md", "approved\n")),
        reply(text="Authorized notes ready"),
    ])

    async def complete(request, cancel):
        return next(responses)

    async def exercise():
        definition = AgentDefinition("demo", project, "scripted", complete)
        async with AgentRuntime.open(tmp_path / "state", [definition], observe=False) as runtime:
            agent = runtime.agent("demo")
            receipt = await agent.submit(
                "Only update README.md and verify NOTES.md still exists",
                acceptance=[FileCheck("README.md", equals="ready\n"), FileCheck("NOTES.md")],
                allowed_write_paths=["README.md"],
            )
            task = await agent.wait(receipt.task_id)
            assert task.state is TaskState.COMPLETED
            assert task.allowed_write_paths == {"README.md"}
            assert (task.workspace / "NOTES.md").read_text() == "original\n"
            assert "NOTES.md" not in task.delivery.diff

            await agent.send("Check the existing notes too", task_id=task.id,
                             acceptance=[FileCheck("NOTES.md", equals="original\n")])
            task = await agent.wait(task.id)
            assert task.state is TaskState.COMPLETED
            assert task.allowed_write_paths == {"README.md"}
            assert (task.workspace / "NOTES.md").read_text() == "original\n"

            await agent.send("Authorize updating NOTES.md", task_id=task.id, message_id="confirmation",
                             acceptance=[FileCheck("NOTES.md", equals="approved\n")],
                             allowed_write_paths=["README.md", "NOTES.md"])
            task = await agent.wait(task.id)
            assert task.state is TaskState.COMPLETED
            assert task.allowed_write_paths == {"README.md", "NOTES.md"}
            actions = {action.call.call_id: action for action in agent.actions(task.id)}
            assert actions["extra"].result.is_error and actions["extra"].attempts == 0
            assert actions["still-extra"].result.is_error and actions["still-extra"].attempts == 0
            assert not actions["confirmed"].result.is_error
            (revision,) = [entry for entry in agent.history(task.id) if entry.kind == "write_scope_revised"]
            assert revision.data["input_id"] == "confirmation"
            assert revision.data["previous_paths"] == ["README.md"]
            assert revision.data["allowed_write_paths"] == ["NOTES.md", "README.md"]

    asyncio.run(exercise())


def test_task_saved_before_write_scope_keeps_delivery_and_requires_new_authorization(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    responses = iter([
        reply(write("readme", "README.md", "ready\n")),
        reply(text="Original delivery"),
        reply(write("extra", "README.md", "revoked\n")),
        reply(text="Checked the existing delivery"),
        reply(text="README still matches"),
        reply(text="README still matches"),
    ])

    async def complete(request, cancel):
        return next(responses)

    async def exercise():
        state = tmp_path / "state"
        definition = AgentDefinition("demo", project, "scripted", complete)
        async with AgentRuntime.open(state, [definition], observe=False) as runtime:
            agent = runtime.agent("demo")
            receipt = await agent.submit("Write README.md", acceptance=[FileCheck("README.md", equals="ready\n")],
                                         allowed_write_paths=["README.md"])
            original = await agent.wait(receipt.task_id)
            assert original.state is TaskState.COMPLETED
        db = sqlite3.connect(state / "agent.sqlite3")
        try:
            db.execute("ALTER TABLE tasks DROP COLUMN allowed_write_paths")
        finally:
            db.close()
        async with AgentRuntime.open(state, [definition], observe=False) as runtime:
            agent = runtime.agent("demo")
            task = agent.task(original.id)
            assert task.delivery == original.delivery and task.allowed_write_paths == set()
            await agent.send("Revoke permission to write README.md", task_id=task.id, allowed_write_paths=[])
            task = await agent.wait(task.id)
            assert task.allowed_write_paths == set()
            assert task.reason is RunReason.VERIFICATION_FAILED
            assert (task.workspace / "README.md").read_text() == "ready\n"
            assert agent.actions(task.id)[-1].result.is_error
            (completed,) = [entry for entry in agent.history(task.id) if entry.kind == "completed"]
            assert completed.data["delivery"]["text"] == "Original delivery"

    asyncio.run(exercise())


@pytest.mark.parametrize("workspace", ["copy", "git", "staged"])
def test_verification_rejects_an_unexpected_file(tmp_path, workspace):
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("accepted\n")
    if workspace != "copy":
        (project / "NOTES.md").write_text("original\n")
        for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "base")):
            subprocess.run(["git", "-C", str(project), "-c", "user.name=Demo",
                            "-c", "user.email=demo@example.invalid", *args], check=True)

    async def complete(request, cancel):
        root = agent.tasks()[0].workspace
        notes = root / "NOTES.md"
        if workspace == "staged":
            notes.write_text("unauthorized\n")
            subprocess.run(["git", "-C", str(root), "add", "NOTES.md"], check=True)
            notes.write_text("original\n")
        else:
            notes.write_bytes(b"")
        return reply(text="README still passes")

    async def exercise():
        nonlocal agent
        definition = AgentDefinition("demo", project, "scripted", complete)
        async with AgentRuntime.open(tmp_path / "state", [definition], observe=False) as runtime:
            agent = runtime.agent("demo")
            receipt = await agent.submit("Verify README.md", acceptance=[FileCheck("README.md", equals="accepted\n")],
                                         budget=Budget(verifications=1), allowed_write_paths=[])
            task = await agent.wait(receipt.task_id)
            assert task.allowed_write_paths == set()
            assert task.state is TaskState.FAILED
            assert task.reason is RunReason.VERIFICATION_FAILED
            assert "NOTES.md" in task.detail
            assert task.delivery is None
            (verified,) = [entry for entry in agent.history(task.id) if entry.kind == "failed"]
            assert verified.data["checks"][0]["verdict"] == "passed"

    agent = None
    asyncio.run(exercise())


def test_recovery_keeps_a_persisted_plan_update(tmp_path, monkeypatch):
    from demoharness.core.storage import TaskStore

    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").touch()

    async def complete(request, cancel):
        if any(m.get("tool_call_id") == "review-plan" for m in request.messages):
            return reply(text="Reviewed")
        return reply(tool("review-plan", "set_plan", steps=["Inspect the report"], reason="Review process",
                          expected_revision=1))

    save_result = TaskStore.result

    def interrupt_after_plan(store, run, action, result):
        if action.call.call_id == "review-plan":
            raise asyncio.CancelledError
        return save_result(store, run, action, result)

    async def exercise():
        definition = AgentDefinition("demo", project, "scripted", complete)
        state = tmp_path / "state"
        with monkeypatch.context() as patch:
            patch.setattr(TaskStore, "result", interrupt_after_plan)
            async with AgentRuntime.open(state, [definition], observe=False) as runtime:
                agent = runtime.agent("demo")
                receipt = await agent.submit("Review README.md", acceptance=[FileCheck("README.md")])
                task = await agent.wait(receipt.task_id)
                assert task.state is TaskState.QUEUED and task.plan.revision == 2
        async with AgentRuntime.open(state, [definition], observe=False) as runtime:
            agent = runtime.agent("demo")
            task = await agent.wait(receipt.task_id)
            assert task.state is TaskState.COMPLETED and task.plan.revision == 2
            (action,) = agent.actions(task.id)
            assert action.attempts == 1 and not action.result.is_error
            assert json.loads(action.result.content)["revision"] == 2
            (revision,) = [entry for entry in agent.history(task.id) if entry.kind == "plan_revised"]
            assert revision.data["action_id"] == action.id

    asyncio.run(exercise())


@pytest.mark.parametrize("message, expected_scope", [
    ("授权写入：README.md | 更新说明，保留其他文件", {"README.md"}),
    ("Write NOTES.md, but do not change any file yet. First explain your plan.", set()),
    ("Do not modify NOTES.md. Please review README.md.", set()),
    ('Someone wrote "Create NOTES.md" in README.md. Review the reference.', set()),
])
def test_coordinator_uses_user_directive_for_scope(tmp_path, message, expected_scope):
    project = tmp_path / "project"
    project.mkdir()
    (project / "NOTES.md").touch()

    async def complete(request, cancel):
        if "你负责判断消息属于哪件事" in request.system_prompt:
            return reply(text=json.dumps({"kind": "create", "text": "Create NOTES.md", "checks": [{"path": "NOTES.md"}]}))
        if any(m["role"] == "tool" for m in request.messages):
            return reply(text="Notes exist")
        return reply(write("model-proposal", "NOTES.md", "unconfirmed\n"))

    async def exercise():
        definition = AgentDefinition("demo", project, "scripted", complete)
        async with AgentRuntime.open(tmp_path / "state", [definition], observe=False) as runtime:
            agent = runtime.agent("demo")
            await agent.send(message, message_id="chat")
            async with asyncio.timeout(5):
                while not agent.tasks():
                    await asyncio.sleep(0.01)
            task = await agent.wait(agent.input("chat").task_id)
            assert task.state is TaskState.COMPLETED
            assert task.allowed_write_paths == expected_scope
            assert (task.workspace / "NOTES.md").read_bytes() == b""
            (action,) = agent.actions(task.id)
            assert action.result.is_error

    asyncio.run(exercise())


def test_plan_retires_a_step_with_evidence_and_rejects_stale_revisions(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("accepted\n")
    goal = "Review README.md"

    async def complete(request, cancel):
        plan_text = next(line for line in request.system_prompt.splitlines() if line.startswith("Current Plan: "))
        plan = json.loads(plan_text.removeprefix("Current Plan: "))
        results = {m.get("tool_call_id") for m in request.messages if m["role"] == "tool"}
        step_id = next(step["id"] for step in plan["steps"] if step["action"] == goal)
        if "set" not in results:
            return reply(tool("set", "set_plan", steps=[goal, "Deliver the review"], reason="Expand the plan",
                              expected_revision=plan["revision"]))
        if "mark" not in results:
            return reply(tool("mark", "mark_step", step_id=step_id, state="done", evidence=["README.md"],
                              expected_revision=plan["revision"]),
                         tool("stale", "set_plan", steps=["Stale replacement"], reason="Attempt old revision",
                              expected_revision=plan["revision"]))
        if "retire" not in results:
            return reply(tool("retire", "set_plan", steps=["Deliver the review"], reason="Review is complete",
                              expected_revision=plan["revision"]))
        if "retired" not in results:
            return reply(tool("retired", "mark_step", step_id=step_id, state="running", evidence=[],
                              expected_revision=plan["revision"]))
        return reply(text="README reviewed")

    async def exercise():
        definition = AgentDefinition("demo", project, "scripted", complete)
        state = tmp_path / "state"
        async with AgentRuntime.open(state, [definition], observe=False) as runtime:
            agent = runtime.agent("demo")
            receipt = await agent.submit(goal, acceptance=[FileCheck("README.md")])
            task = await agent.wait(receipt.task_id)
            assert task.state is TaskState.COMPLETED
            actions = {action.call.call_id: action for action in agent.actions(task.id)}
            assert actions["stale"].result.is_error
            assert "Current revision is 3" in actions["stale"].result.content
            assert actions["retired"].result.is_error
            revisions = [entry.data for entry in agent.history(task.id) if entry.kind == "plan_revised"]
            assert [revision["expected_revision"] for revision in revisions] == [1, 2, 3]
            assert revisions[-1]["reason"] == "Review is complete"
        async with AgentRuntime.open(state, [definition], observe=False) as runtime:
            plan = runtime.agent("demo").task(task.id).plan
            active, retired = plan.steps
            assert plan.revision == 4 and active.action == "Deliver the review"
            assert retired.id == actions["mark"].call.params["step_id"]
            assert retired.state is StepState.RETIRED and retired.evidence == ("README.md",)

    asyncio.run(exercise())
