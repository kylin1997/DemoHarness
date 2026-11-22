import asyncio
import json

from demoharness import AgentDefinition, AgentRuntime, TaskState
from demoharness.core.loop import ModelResponse, ModelStop


def text_response(text):
    return ModelResponse(ModelStop.OK, text)


def tool_response(call_id, name, **params):
    return ModelResponse(ModelStop.OK, "", [{"call_id": call_id, "name": name, "params": params}])


def test_new_work_and_follow_up_progress_while_another_task_is_waiting(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    async def exercise():
        slow = asyncio.Event()
        release = asyncio.Event()
        written = asyncio.Event()
        stale_call = False

        async def complete(request, cancel):
            nonlocal stale_call
            if "你负责判断消息属于哪件事" in request.system_prompt:
                message = json.loads(request.messages[-1]["content"])["text"]
                kind = "create" if message.startswith("创建 result.txt") else "clarify"
                result = {"kind": kind, "text": message.split("，", 1)[-1] if kind == "create" else "你指哪件事？"}
                if kind == "create":
                    result["checks"] = [{"path": "result.txt"}]
                return text_response(json.dumps(result, ensure_ascii=False))
            users = [m["content"] for m in request.messages if m["role"] == "user"]
            results = [m for m in request.messages if m["role"] == "tool"]
            if "第二件事" in " ".join(users):
                if results:
                    written.set()
                    return text_response("第二件事已完成")
                return tool_response("b1", "write_file", path="result.txt", content="B")
            if "改用新内容" in " ".join(users):
                if any(m.get("tool_call_id") == "new" for m in results):
                    return text_response("第一件事已更新")
                if any(m.get("tool_call_id") == "plan" for m in results):
                    return tool_response("new", "write_file", path="result.txt", content="A updated")
                return tool_response("plan", "set_plan", steps=["写入新内容"], reason="用户补充要求", expected_revision=1)
            if stale_call:
                return text_response("模型没有看到新内容")
            stale_call = True
            slow.set()
            await release.wait()
            return tool_response("old", "write_file", path="result.txt", content="A stale")

        async with AgentRuntime.open(tmp_path / "state", [AgentDefinition("demo", project, "scripted", complete)],
                                     observe=False) as runtime:
            agent = runtime.agent("demo")
            await agent.send("创建 result.txt，第一件事", message_id="a")
            await asyncio.wait_for(slow.wait(), timeout=5)
            task_a = next(task for task in agent.tasks() if task.goal == "第一件事")
            await agent.send("创建 result.txt，第二件事", message_id="b")
            await agent.send("创建 result.txt，第二件事", message_id="b")
            await asyncio.wait_for(written.wait(), timeout=5)
            task_b = next(task for task in agent.tasks() if task.goal == "第二件事")
            assert (await agent.wait(task_b.id)).state is TaskState.COMPLETED
            await agent.send("改用新内容", task_id=task_a.id, message_id="followup")
            release.set()
            task_a = await asyncio.wait_for(agent.wait(task_a.id), timeout=5)
            assert task_a.state is TaskState.COMPLETED
            assert len(agent.tasks()) == 2
            assert (task_b.workspace / "result.txt").read_text() == "B"
            assert (task_a.workspace / "result.txt").read_text() == "A updated"
            assert not (project / "result.txt").exists()
            assert task_a.plan.steps[0].action == "写入新内容"
            assert not any(action.call.call_id == "old" and action.attempts > 0
                           for action in agent.actions(task_a.id))

    asyncio.run(exercise())


def test_confirmed_acceptance_resumes_the_same_waiting_task(tmp_path):
    from demoharness import FileCheck

    project = tmp_path / "project"
    project.mkdir()
    (project / "result.txt").write_text("existing evidence")

    async def complete(request, cancel):
        return text_response("检查已完成。")

    async def exercise():
        definition = AgentDefinition("demo", project, "scripted", complete)
        async with AgentRuntime.open(tmp_path / "state", [definition]) as runtime:
            agent = runtime.agent("demo")
            receipt = await agent.submit("复核已有结果")
            waiting = await agent.wait(receipt.task_id)
            assert waiting.state is TaskState.WAITING
            await agent.send("按现有文件内容验收", task_id=waiting.id,
                             acceptance=[FileCheck("result.txt", equals="existing evidence")], goal="复核结果内容")
            delivered = await agent.wait(waiting.id)
            assert delivered.state is TaskState.COMPLETED
            assert delivered.session_id == waiting.session_id
            assert delivered.goal == "复核结果内容"
            assert delivered.delivery.checks[0].path == "result.txt"

    asyncio.run(exercise())
