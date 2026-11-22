from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from demoharness.core.loop import ModelResponse, ModelStop


def call(call_id, name, **params):
    return {"call_id": call_id, "name": name, "params": params}


class Script:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    async def __call__(self, request, cancel):
        self.requests.append(request)
        return next(self.responses)


def tools(*calls):
    return ModelResponse(ModelStop.OK, "", list(calls))


def done(text="Done"):
    return ModelResponse(ModelStop.OK, text)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "source.py").write_text("source = 1\n")
    git(root, "add", "source.py")
    git(
        root,
        "-c",
        "user.name=P1",
        "-c",
        "user.email=p1@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    return root


def test_file_task_corrects_failed_acceptance_and_delivers_worktree(repo, tmp_path):
    from demoharness import AgentDefinition, AgentRuntime, Budget, FileCheck, TaskState

    script = Script(
        done("It's already done"),
        tools(
            call("write-1", "write_file", path="nested/out.py", content="print(1)\n")
        ),
        done(),
    )

    async def scenario():
        definition = AgentDefinition("a", repo, "scripted", script)
        async with AgentRuntime.open(tmp_path / "state", [definition], observe=False) as runtime:
            agent = runtime.agent("a")
            receipt = await agent.submit(
                "Create nested/out.py",
                acceptance=[
                    FileCheck("nested/out.py", equals="print(1)\n", python_syntax=True)
                ],
                budget=Budget(model_calls=3, tool_calls=1),
                input_id="request-1",
            )
            task = await agent.wait(receipt.task_id)
            assert (
                await agent.submit("Create nested/out.py", input_id="request-1")
                == receipt
            )
            assert task.state == TaskState.COMPLETED
            assert task.base_commit == git(repo, "rev-parse", "HEAD")
            assert git(task.workspace, "rev-parse", "--git-common-dir") == str(
                repo / ".git"
            )
            assert (task.workspace / "nested/out.py").read_text() == "print(1)\n"
            assert not (repo / "nested").exists()
            assert task.delivery.checks[0].sha256
            assert "+++ b/nested/out.py" in task.delivery.diff
            assert task.budget.model_calls == task.budget.tool_calls == 0
            assert agent.runs(task.id)[0].reason.value == "completed"
            assert [item.kind for item in agent.history(task.id)].count(
                "completed"
            ) == 1
            assert agent.actions(task.id)[0].result.content
            assert agent.notices(task.id)[0].kind == "completed"
        assert len(script.requests) == 3
        assert "Verification failed" in script.requests[1].messages[-1]["content"]
        assert script.requests[-1].messages[-1]["tool_call_id"] == "write-1"

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["worktree", "fingerprint", "verify", "diff"])
def test_blocked_workspace_io_allows_other_tasks_and_input(repo, tmp_path, monkeypatch, operation):
    from demoharness import AgentDefinition, AgentRuntime, FileCheck, TaskState
    from demoharness.tools import files

    entered, release = threading.Event(), threading.Event()
    slow_workspace = None

    def block():
        if not entered.is_set():
            entered.set()
            assert release.wait(5), f"{operation} stalled the event loop"

    git_command = files._git
    read = files.Workspace.read

    def delayed_git(root, lock_fd, *args, **kwargs):
        if operation == "worktree" and args[:2] == ("worktree", "add") and str(slow_workspace) in args:
            block()
        if operation == "diff" and root == slow_workspace and args[0] == "diff":
            block()
        return git_command(root, lock_fd, *args, **kwargs)

    def delayed_read(workspace, path):
        if workspace.root == slow_workspace and (
            (operation == "fingerprint" and path == "obsolete.py")
            or (operation == "verify" and path == "source.py")
        ):
            block()
        return read(workspace, path)

    monkeypatch.setattr(files, "_git", delayed_git)
    monkeypatch.setattr(files.Workspace, "read", delayed_read)

    async def model(request, cancel):
        if operation == "fingerprint" and "Current Task goal: Slow\n" in request.system_prompt:
            return tools(call("obsolete", "write_file", path="obsolete.py", content="obsolete = 1\n"))
        return done()

    async def scenario():
        nonlocal slow_workspace
        definition = AgentDefinition("a", repo, "scripted", model)
        check = FileCheck("source.py", equals="source = 1\n")
        updated_check = FileCheck("source.py", contains="source = 1", id="follow-up")
        async with AgentRuntime.open(tmp_path / "state", [definition], observe=False) as runtime:
            agent = runtime.agent("a")
            slow = await agent.submit("Slow", acceptance=[check], allowed_write_paths=["source.py", "obsolete.py"])
            slow_workspace = agent.task(slow.task_id).workspace
            try:
                async with asyncio.timeout(5):
                    while not entered.is_set():
                        await asyncio.sleep(0.01)
                    assert agent.task(slow.task_id).state is TaskState.RUNNING
                    follow_up = await agent.send(
                        "Only check source.py", task_id=slow.task_id, goal="Check existing source",
                        acceptance=[updated_check],
                    )
                    assert agent.input(follow_up).text == "Only check source.py"
                    fast = await agent.submit("Fast", acceptance=[check])
                    assert (await agent.wait(fast.task_id)).state is TaskState.COMPLETED
                    assert agent.task(slow.task_id).state is TaskState.RUNNING
            finally:
                release.set()
            task = await agent.wait(slow.task_id)
            assert task.state is TaskState.COMPLETED
            assert task.delivery.checks[0].id == updated_check.id
            assert task.delivery.checks[0].sha256
            if operation == "fingerprint":
                assert agent.actions(task.id)[0].attempts == 0
                assert not (task.workspace / "obsolete.py").exists()
            if operation in {"verify", "diff"}:
                assert any(entry.kind == "verification_superseded" for entry in agent.history(task.id))

    asyncio.run(scenario())


def test_shutdown_waits_for_workspace_write_before_releasing_host_lock(repo, tmp_path, monkeypatch):
    from demoharness import (
        AgentDefinition,
        AgentRuntime,
        Budget,
        FileCheck,
        RunReason,
        TaskState,
    )
    from demoharness.core.storage import StorageAlreadyOpen
    from demoharness.tools.files import Workspace

    entered, release = threading.Event(), threading.Event()
    write = Workspace.write

    def delayed_write(workspace, path, data, before):
        write(workspace, path, data, before)
        entered.set()
        assert release.wait(5), "write stalled the event loop"

    monkeypatch.setattr(Workspace, "write", delayed_write)
    script = Script(tools(call("write", "write_file", path="output.py", content="value = 7\n")), done())
    definition = AgentDefinition("a", repo, "scripted", script)
    state = tmp_path / "state"

    async def scenario():
        stopping = asyncio.Event()

        async def start_and_pause():
            async with AgentRuntime.open(state, [definition], observe=False) as runtime:
                agent = runtime.agent("a")
                receipt = await agent.submit(
                    "Write output.py", acceptance=[FileCheck("output.py", equals="value = 7\n")],
                    budget=Budget(model_calls=2, tool_calls=1),
                )
                async with asyncio.timeout(5):
                    while not entered.is_set():
                        await asyncio.sleep(0.01)
                    assert agent.task(receipt.task_id).state is TaskState.RUNNING
                stopping.set()
            return receipt.task_id

        host = asyncio.create_task(start_and_pause())
        try:
            async with asyncio.timeout(5):
                await stopping.wait()
                finished, _ = await asyncio.wait({host}, timeout=0.05)
                assert not finished
                with pytest.raises(StorageAlreadyOpen):
                    async with AgentRuntime.open(state, [definition], observe=False):
                        pytest.fail("another host took the lock during an in-flight write")
        finally:
            release.set()
            task_id = await host

        async with AgentRuntime.open(state, [definition], observe=False) as runtime:
            agent = runtime.agent("a")
            task = await agent.wait(task_id)
            assert task.state is TaskState.COMPLETED
            assert (task.workspace / "output.py").read_text() == "value = 7\n"
            assert agent.actions(task_id)[0].attempts == 1
            assert agent.runs(task_id)[0].reason is RunReason.PAUSED

    asyncio.run(scenario())


_CRASH_PROGRAM = """
import asyncio
import os
import signal
import sys
from pathlib import Path

from demoharness import AgentDefinition, AgentRuntime, Budget, FileCheck
from demoharness.core.loop import ModelResponse, ModelStop
from demoharness.tools.files import Workspace

original_write = Workspace.write

def interrupt(self, path, data, before):
    if sys.argv[4] == "after":
        original_write(self, path, data, before)
    os.kill(os.getpid(), signal.SIGKILL)

Workspace.write = interrupt

async def model(request, cancel):
    return ModelResponse(ModelStop.OK, "", [{
        "call_id": "process-write", "name": "write_file",
        "params": {"path": "crash.py", "content": "value = 7\\n"},
    }])

async def run():
    async with AgentRuntime.open(sys.argv[1], [AgentDefinition("a", sys.argv[2], "scripted", model)], observe=False) as runtime:
        agent = runtime.agent("a")
        receipt = await agent.submit("Write crash.py", acceptance=[FileCheck("crash.py", equals="value = 7\\n")],
                                     budget=Budget(model_calls=2, tool_calls=int(sys.argv[5])))
        Path(sys.argv[3]).write_text(receipt.task_id)
        await agent.wait(receipt.task_id)

asyncio.run(run())
"""


@pytest.mark.parametrize("ending", ["after", "before", "unknown", "superseded"])
def test_real_process_crash_reconciles_write_and_budget(repo, tmp_path, ending):
    from demoharness import (
        ActionState,
        AgentDefinition,
        AgentRuntime,
        FileCheck,
        RunReason,
        TaskState,
    )

    state, receipt_path = tmp_path / "state", tmp_path / "receipt"
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            _CRASH_PROGRAM,
            str(state),
            str(repo),
            str(receipt_path),
            "before" if ending in {"before", "superseded"} else "after",
            "2" if ending in {"before", "superseded"} else "1",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
    )
    assert process.returncode == -9, process.stderr
    task_id = receipt_path.read_text()
    output = state / "agents" / "a" / "workspaces" / task_id / "crash.py"
    if ending in {"before", "superseded"}:
        assert not output.exists()
    else:
        assert output.read_text() == "value = 7\n"
    if ending == "unknown":
        output.write_text("unrelated = True\n")
    inode_before = output.stat().st_ino if output.exists() else None
    script = Script(done())

    async def reopen():
        definition = AgentDefinition("a", repo, "scripted", script)
        async with AgentRuntime.open(state, [definition], observe=False) as runtime:
            agent = runtime.agent("a")
            if ending == "superseded":
                await agent.send("不要创建 crash.py，只检查已有的 source.py", task_id=task_id,
                                 acceptance=[FileCheck("source.py")])
            task = await agent.wait(task_id)
            old, recovered = agent.runs(task_id)
            assert old.reason is RunReason.INTERRUPTED
            (action,) = agent.actions(task_id)
            assert action.run_id == old.id
            assert action.call.call_id == "process-write"
            assert action.id.startswith(f"{old.id}:{action.response_id}:")
            if ending == "superseded":
                assert task.state is TaskState.COMPLETED, task.detail
                assert action.attempts == 1 and task.budget.tool_calls == 1
                assert not output.exists()
                return
            assert task.budget.tool_calls == 0
            action_entries = [
                entry
                for entry in agent.history(task_id)
                if entry.kind.startswith("action_")
            ]
            assert {entry.data["action_id"] for entry in action_entries} == {action.id}
            if ending == "unknown":
                assert task.state is TaskState.WAITING
                assert recovered.reason is RunReason.UNKNOWN_EFFECT
                assert action.state is ActionState.STARTED
                assert action.attempts == 1
                assert [entry.kind for entry in action_entries] == [
                    "action_pending",
                    "action_started",
                ]
                assert task.budget.model_calls == 1
                assert output.read_text() == "unrelated = True\n"
            else:
                assert task.state is TaskState.COMPLETED, task.detail
                assert recovered.reason is RunReason.COMPLETED
                assert action.state is ActionState.RESULT
                assert action.result.details["recovered"] is (ending == "after")
                assert action.attempts == (2 if ending == "before" else 1)
                assert [entry.kind for entry in action_entries].count(
                    "action_result"
                ) == 1
                assert task.budget.model_calls == 0
                assert output.read_text() == "value = 7\n"
        if ending == "unknown":
            async with AgentRuntime.open(state, [definition], observe=False) as runtime:
                agent = runtime.agent("a")
                assert (await agent.wait(task_id)).state is TaskState.WAITING
                assert len(agent.runs(task_id)) == 2

    asyncio.run(reopen())
    if ending == "unknown":
        assert not script.requests
    elif ending != "superseded":
        assert len(script.requests) == 1
        assert script.requests[0].messages[-1]["tool_call_id"] == "process-write"
    if inode_before is not None:
        assert output.stat().st_ino == inode_before


def test_budget_blocks_second_tool_then_resumes_same_call_batch(repo, tmp_path):
    from demoharness import (
        ActionState,
        AgentDefinition,
        AgentRuntime,
        Budget,
        FileCheck,
        RunReason,
        TaskState,
    )

    script = Script(
        tools(
            call("first", "write_file", path="one.py", content="one = 1\n"),
            call("second", "write_file", path="two.py", content="two = 2\n"),
        ),
        done(),
    )

    async def scenario():
        definition = AgentDefinition("a", repo, "scripted", script)
        state = tmp_path / "state"
        async with AgentRuntime.open(state, [definition], observe=False) as runtime:
            agent = runtime.agent("a")
            receipt = await agent.submit(
                "Write one.py and two.py",
                acceptance=[FileCheck("two.py")],
                allowed_write_paths=["one.py", "two.py"],
                budget=Budget(model_calls=2, tool_calls=1),
            )
            task = await agent.wait(receipt.task_id)
            assert task.state is TaskState.WAITING
            assert task.reason is RunReason.BUDGET_EXHAUSTED
            assert (task.workspace / "one.py").exists()
            assert not (task.workspace / "two.py").exists()
            assert [a.state for a in agent.actions(task.id)] == [
                ActionState.RESULT,
                ActionState.PENDING,
            ]
            assert len(script.requests) == 1
        async with AgentRuntime.open(state, [definition], observe=False) as runtime:
            agent = runtime.agent("a")
            assert (await agent.resume(task.id)).state is TaskState.WAITING
            assert len(script.requests) == 1
            await agent.resume(
                task.id,
                budget=Budget(
                    model_calls=0, tool_calls=1, verifications=0, compactions=0
                ),
            )
            assert (await agent.wait(task.id)).state is TaskState.COMPLETED
            assert [a.attempts for a in agent.actions(task.id)] == [1, 1]
        assert [
            message["tool_call_id"]
            for message in script.requests[-1].messages
            if message["role"] == "tool"
        ] == ["first", "second"]

    asyncio.run(scenario())


def test_distinct_stopped_failed_and_model_budget_states_persist(repo, tmp_path):
    from demoharness import (
        AgentDefinition,
        AgentRuntime,
        Budget,
        FileCheck,
        RunReason,
        TaskState,
    )

    async def scenario():
        started = asyncio.Event()
        blocked_calls = []

        async def blocked(request, cancel):
            blocked_calls.append(request)
            started.set()
            await asyncio.Future()

        state = tmp_path / "state"
        stopped_definition = AgentDefinition("a", repo, "scripted", blocked)
        failure = Script(done())
        zero = Script()
        failed_definition = AgentDefinition("b", repo, "scripted", failure)
        zero_definition = AgentDefinition("c", repo, "scripted", zero)
        async with AgentRuntime.open(
            state, [stopped_definition, failed_definition, zero_definition], observe=False
        ) as runtime:
            a, b, c = (runtime.agent(name) for name in "abc")
            stopped = await a.submit(
                "Stop in the model",
                acceptance=[FileCheck("source.py")],
                budget=Budget(model_calls=2),
            )
            await asyncio.wait_for(started.wait(), 3)
            assert (await a.stop(stopped.task_id)).state is TaskState.STOPPED
            assert a.runs(stopped.task_id)[0].reason is RunReason.STOPPED
            failed = await b.submit(
                "Accept a missing file",
                acceptance=[FileCheck("missing.py")],
                budget=Budget(model_calls=1, verifications=1),
            )
            result = await b.wait(failed.task_id)
            assert result.state is TaskState.FAILED
            assert result.reason is RunReason.VERIFICATION_FAILED
            assert result.delivery is None
            assert b.notices(result.id)[0].data["checks"][0]["verdict"] == "failed"
            capped = await c.submit(
                "No model calls",
                acceptance=[FileCheck("source.py")],
                budget=Budget(model_calls=0),
            )
            assert (await c.wait(capped.task_id)).reason is RunReason.BUDGET_EXHAUSTED
            with pytest.raises(KeyError):
                c.task(stopped.task_id)
        assert not zero.requests
        replacement = Script(done())
        async with AgentRuntime.open(
            state, [AgentDefinition("a", repo, "scripted", replacement)], observe=False
        ) as runtime:
            a = runtime.agent("a")
            assert (await a.wait(stopped.task_id)).state is TaskState.STOPPED
            assert not replacement.requests
            assert a.task(stopped.task_id).budget.model_calls == 1
            await a.resume(stopped.task_id)
            assert (await a.wait(stopped.task_id)).state is TaskState.COMPLETED
            assert [run.reason for run in a.runs(stopped.task_id)] == [
                RunReason.STOPPED,
                RunReason.COMPLETED,
            ]

    asyncio.run(scenario())


def test_workspace_paths_and_tools_are_enforced_in_plain_project(tmp_path):
    from demoharness import AgentDefinition, AgentRuntime, FileCheck, TaskState

    project = tmp_path / "plain"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (project / "link").symlink_to(outside)
    (project / "directory-link").symlink_to(tmp_path, target_is_directory=True)
    (project / "source.py").write_text("old = 1\n")
    bad_calls = [
        call("absolute", "read_file", path=str(outside)),
        call("escape", "write_file", path="../../../outside.txt", content="bad"),
        call("git", "write_file", path="sub/.git/config", content="bad"),
        call("link", "read_file", path="link"),
        call(
            "directory-link",
            "write_file",
            path="directory-link/outside.txt",
            content="bad",
        ),
        call("shell", "run_command", command="touch outside.txt"),
        call("unknown", "run_check", recipe_id="shell"),
    ]
    script = Script(
        tools(
            *bad_calls,
            call("write", "write_file", path="out.py", content="pass\n"),
            call("check", "run_check", recipe_id="syntax"),
        ),
        done(),
    )

    async def scenario():
        definition = AgentDefinition("a", project, "scripted", script)
        # A state directory inside a plain project is excluded from the snapshot.
        async with AgentRuntime.open(project / "state", [definition], observe=False) as runtime:
            a = runtime.agent("a")
            receipt = await a.submit(
                "Write Python safely",
                acceptance=[FileCheck("out.py", python_syntax=True, id="syntax")],
            )
            result = await a.wait(receipt.task_id)
            assert result.state is TaskState.COMPLETED, result.detail
            assert result.workspace_kind == "copy"
            assert not (result.workspace / "state").exists()
            assert (result.workspace / "source.py").read_text() == "old = 1\n"
            assert not (project / "out.py").exists()
            assert outside.read_text() == "private"
            assert "+++ b/out.py" in result.delivery.diff
            actions = a.actions(result.id)
            assert all(action.result.is_error for action in actions[: len(bad_calls)])
            assert all(
                not action.result.is_error for action in actions[len(bad_calls) :]
            )
            assert actions[-1].result.details["verdict"] == "passed"
        assert {spec.name for spec in script.requests[0].tools} == {
            "read_file",
            "write_file",
            "run_check",
            "set_plan",
            "mark_step",
            "delegate",
        }

    asyncio.run(scenario())


def test_compaction_keeps_full_recent_tool_groups_and_original_history(repo, tmp_path):
    from demoharness import AgentDefinition, AgentRuntime, FileCheck, TaskState

    batches = [
        tools(*(call(f"{i}-{j}", "read_file", path="source.py") for j in range(2)))
        for i in range(3)
    ]
    script = Script(*batches, done())

    async def scenario():
        definition = AgentDefinition(
            "a", repo, "scripted", script, max_tokens=100, context_messages=3
        )
        async with AgentRuntime.open(tmp_path / "state", [definition], observe=False) as runtime:
            a = runtime.agent("a")
            receipt = await a.submit(
                "Read and verify source",
                acceptance=[FileCheck("source.py", equals="source = 1\n")],
            )
            task = await a.wait(receipt.task_id)
            assert task.state is TaskState.COMPLETED
            history = a.history(task.id)
            assert (
                len([entry for entry in history if entry.kind == "action_result"]) == 6
            )
            assert history[0].data["text"] == "Read and verify source"
            summary = [entry for entry in history if entry.kind == "summary"]
            assert summary and summary[-1].data["through"] > history[0].id
            context = script.requests[-1].messages
            assert context[0]["content"].startswith("Earlier Session summary")
            assert script.requests[-1].max_tokens == 100
            assert "Acceptance checks" in script.requests[-1].system_prompt
            for index, message in enumerate(context):
                if "tool_calls" in message:
                    ids = [call["id"] for call in message["tool_calls"]]
                    assert [
                        context[index + offset]["tool_call_id"] for offset in (1, 2)
                    ] == ids
            assert [
                message["tool_call_id"]
                for message in context
                if message["role"] == "tool"
            ] == ["1-0", "1-1", "2-0", "2-1"]

    asyncio.run(scenario())
