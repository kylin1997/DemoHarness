import asyncio
import json
import sqlite3
import subprocess

import pytest

from demoharness import AgentDefinition, AgentRuntime, FileCheck, TaskState
from demoharness.coordination import Route
from demoharness.core.loop import ModelResponse, ModelStop


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def test_completed_task_teaches_only_its_own_agent(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "runner.py").write_text("run = True\n")
    prompts = {"a": [], "b": []}

    def model(aid):
        async def complete(request, cancel):
            prompts[aid].append(request.system_prompt)
            return ModelResponse(ModelStop.OK, "检查完成。")
        return complete

    async def exercise():
        definitions = [AgentDefinition(aid, project, "scripted", model(aid)) for aid in "ab"]
        async with AgentRuntime.open(tmp_path / "state", definitions) as runtime:
            a, b = (runtime.agent(aid) for aid in "ab")
            check = FileCheck("runner.py", python_syntax=True)
            first = await a.submit("验证 runner.py 语法", acceptance=[check])
            assert (await a.wait(first.task_id)).state is TaskState.COMPLETED
            await until(a.memories)
            original = a.memories()[0]
            assert original.sources[0].startswith(f"task:{first.task_id}:entry:")
            second = await a.submit("复核 runner.py", acceptance=[check])
            await a.wait(second.task_id)
            assert original.text in prompts["a"][-1]
            other = await b.submit("复核 runner.py", acceptance=[check])
            await b.wait(other.task_id)
            assert original.text not in prompts["b"][-1]
            assert a.delete_memory(original.id)
            assert not any(memory.id == original.id for memory in a.memories())
        async with AgentRuntime.open(tmp_path / "state", definitions) as runtime:
            await asyncio.sleep(0.03)
            assert not any(memory.id == original.id for memory in runtime.agent("a").memories())

    asyncio.run(exercise())


def test_repo_change_is_discovered_by_default_and_same_work_is_linked(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)

    git("init", "-q")

    def commit(body):
        (project / "notes.md").write_text(body)
        git("add", "notes.md")
        git("-c", "user.name=Demo", "-c", "user.email=demo@example.invalid", "commit", "-qm", body)

    commit("Initial notes\n")
    observations = []
    failed_once = False

    async def complete(request, cancel):
        nonlocal failed_once
        if "你负责判断消息属于哪件事" not in request.system_prompt:
            return ModelResponse(ModelStop.OK, "已记录检查结果。")
        data = json.loads(request.messages[-1]["content"])
        assert data["kind"] == "project_observation"
        if "TODO" in data["text"] and not failed_once:
            failed_once = True
            return ModelResponse(ModelStop.ERROR, "", error="temporary network failure")
        observations.append(data["text"])
        result = {"kind": "skip"}
        if "TODO" in data["text"]:
            result = {"kind": "create", "text": "Inspect project notes", "work_key": "notes.md:todo",
                      "checks": [{"path": "notes.md", "contains": "TODO"}]}
        return ModelResponse(ModelStop.OK, json.dumps(result))

    async def exercise():
        definition = AgentDefinition("demo", project, "scripted", complete)
        async with AgentRuntime.open(tmp_path / "state", [definition], observe_interval=0.025) as runtime:
            agent = runtime.agent("demo")
            await until(lambda: len(observations) == 1)
            commit("TODO: verify the notes\n")
            await until(agent.tasks)
            task = await agent.wait(agent.tasks()[0].id)
            assert task.state is TaskState.COMPLETED
            assert "write_file" not in task.capabilities
            (task.workspace / "local.md").write_text("TODO: Task output only\n")
            await asyncio.sleep(0.12)
            assert len(observations) == 2
            commit("TODO: verify the notes and report findings\n")
            await until(lambda: len(observations) == 3)
            await until(lambda: len([e for e in agent.history(task.id) if e.kind == "input"]) == 2)
            assert [t.id for t in agent.tasks()] == [task.id]
        async with AgentRuntime.open(tmp_path / "state", [definition], observe_interval=0.025) as runtime:
            await asyncio.sleep(0.12)
            assert len(observations) == 3
            assert [t.id for t in runtime.agent("demo").tasks()] == [task.id]

    asyncio.run(exercise())


def test_observation_route_rolls_back_and_can_be_replayed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    async def complete(request, cancel):
        raise AssertionError("The test applies the selected route")

    async def exercise():
        definition = AgentDefinition("demo", project, "scripted", complete)
        async with AgentRuntime.open(tmp_path / "state", [definition], observe=False) as runtime:
            agent = runtime.agent("demo")
            log = runtime._coordination
            db = runtime._store.db
            route = Route("create", "Inspect notes", work_key="notes.txt:todo")
            work_key = f"{project}:{route.work_key}"

            def fail_disposition():
                db.execute("""
                    CREATE TRIGGER reject_disposition BEFORE UPDATE OF decision ON coordination
                    BEGIN SELECT RAISE(ABORT, 'test write failure'); END
                """)

            log.receive(agent.id, "first", "Initial issue", revision="1" * 40)
            first = log.next(agent.id)
            fail_disposition()
            with pytest.raises(sqlite3.IntegrityError):
                runtime._apply_route(definition, first, route)
            assert agent.tasks() == []
            with pytest.raises(KeyError):
                agent.input(first.id)
            assert log.next(agent.id) == first
            assert log.task_for_work(agent.id, work_key) is None
            assert log.notices(agent.id, 0) == []

            db.execute("DROP TRIGGER reject_disposition")
            runtime._apply_route(definition, first, route)
            runtime._apply_route(definition, first, route)
            task, = agent.tasks()
            assert agent.input(first.id).task_id == log.task_for_work(agent.id, work_key) == task.id
            assert [notice.kind for notice in log.notices(agent.id, 0)] == ["discovered"]
            assert log.next(agent.id) is None
            runtime._store.stop(task.id)

            log.receive(agent.id, "next", "Reproduced issue", revision="2" * 40)
            next_message = log.next(agent.id)
            fail_disposition()
            with pytest.raises(sqlite3.IntegrityError):
                runtime._apply_route(definition, next_message, route)
            with pytest.raises(KeyError):
                agent.input(next_message.id)
            assert log.next(agent.id) == next_message
            assert [notice.kind for notice in log.notices(agent.id, 0)] == ["discovered", "stopped"]

            db.execute("DROP TRIGGER reject_disposition")
            runtime._apply_route(definition, next_message, route)
            runtime._apply_route(definition, next_message, route)
            assert agent.input(next_message.id).task_id == task.id
            assert [entry.kind for entry in agent.history(task.id)].count("input") == 2
            assert [notice.kind for notice in log.notices(agent.id, 0)] == ["discovered", "stopped", "associated"]
            assert agent.task(task.id).state is TaskState.STOPPED
            assert agent.runs(task.id) == []
            assert log.next(agent.id) is None

    asyncio.run(exercise())
