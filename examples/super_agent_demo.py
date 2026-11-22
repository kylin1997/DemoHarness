"""Observe two Tasks, a delegated investigation, memory, and a Git discovery."""

import asyncio
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from demoharness import AgentDefinition, AgentRuntime, FileCheck, TaskState
from demoharness.core.loop import ModelResponse, ModelStop


async def until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.02)


def tool(name, **params):
    return ModelResponse(ModelStop.OK, "", [{"call_id": name, "name": name, "params": params}])


class DemoModel:
    def __init__(self):
        self.baseline = asyncio.Event()
        self.started_a = asyncio.Event()
        self.release_a = asyncio.Event()

    async def __call__(self, request, cancel):
        prompt = request.system_prompt
        results = {m.get("tool_call_id") for m in request.messages if m["role"] == "tool"}
        if "你负责判断消息属于哪件事" in prompt:
            context = json.loads(request.messages[-1]["content"])
            observation = context["text"]
            self.baseline.set()
            decision = {"kind": "skip"}
            if "TODO" in observation:
                assert any("a.txt" in memory for memory in context["memories"])
                decision = {"kind": "create", "text": "检查新提交中的待办", "work_key": "notes:todo",
                            "checks": [{"path": "notes.md", "contains": "TODO"}]}
            return ModelResponse(ModelStop.OK, json.dumps(decision, ensure_ascii=False))
        if "delegated request" in prompt:
            if "read_parent_history" not in results:
                return tool("read_parent_history", limit=20)
            assert any("改写：a.txt 使用新要求" in m["content"] for m in request.messages if m["role"] == "tool")
            return ModelResponse(ModelStop.OK, "已从父 Session 核对补充要求。")
        if "工作 A" in prompt:
            updates = "\n".join(m["content"] for m in request.messages if m["role"] == "user")
            if "改写" not in updates:
                self.started_a.set()
                await self.release_a.wait()
                return tool("write_file", path="a.txt", content="旧要求\n")
            if "set_plan" not in results:
                return tool("set_plan", expected_revision=1, reason="用户补充了新要求",
                            steps=["核对补充", "写入并验证 a.txt"])
            if "delegate" not in results:
                return tool("delegate", prompt="核对父 Session 的新要求")
            if "write_file" not in results:
                return tool("write_file", path="a.txt", content="新要求\n")
        if "工作 B" in prompt and "write_file" not in results:
            return tool("write_file", path="b.txt", content="独立交付\n")
        return ModelResponse(ModelStop.OK, "请验收。")


def commit(project: Path, content: str):
    (project / "notes.md").write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "notes.md"], check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=Demo", "-c", "user.email=demo@example.invalid",
                    "commit", "-qm", "Teaching fixture"], check=True)


async def run(root: Path):
    project = root / "project"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    commit(project, "项目笔记\n")
    model = DemoModel()
    definition = AgentDefinition("demo", project, "scripted", model)
    state = root / "state"
    async with AgentRuntime.open(state, [definition], observe_interval=0.05) as runtime:
        agent = runtime.agent("demo")
        await asyncio.wait_for(model.baseline.wait(), 8)
        a = await agent.submit("工作 A：写 a.txt", acceptance=[FileCheck("a.txt", equals="新要求\n")])
        await asyncio.wait_for(model.started_a.wait(), 8)
        b = await agent.submit("工作 B：写 b.txt", acceptance=[FileCheck("b.txt", equals="独立交付\n")])
        task_b = await asyncio.wait_for(agent.wait(b.task_id), 8)
        assert task_b.state is TaskState.COMPLETED
        print("B 已交付，A 仍在等待模型。", flush=True)
        await agent.send("改写：a.txt 使用新要求", task_id=a.task_id)
        model.release_a.set()
        task_a = await asyncio.wait_for(agent.wait(a.task_id), 8)
        assert task_a.state is TaskState.COMPLETED and task_a.plan.revision > 1, task_a.detail
        delegated = next(action for action in agent.actions(task_a.id) if action.call.name == "delegate")
        assert not delegated.result.is_error
        print(f"A 修订了 Plan 并验收。子 Agent：{delegated.result.details['summary']}", flush=True)
        await until(lambda: any("a.txt" in m.text and any(source.startswith(f"task:{task_a.id}:") for source in m.sources)
                                for m in agent.memories()))
        print("Agent 记忆保存了 a.txt 的检查及来源。", flush=True)
        commit(project, "TODO: 核对 a.txt 的验收方法\n")
        await until(lambda: len(agent.tasks()) == 3)
        discovered = next(t for t in agent.tasks() if t.id not in {task_a.id, task_b.id})
        assert (await asyncio.wait_for(agent.wait(discovered.id), 8)).state is TaskState.COMPLETED
        print(f"新提交触发了 Task，并召回了 a.txt 的验收记忆：{discovered.goal}", flush=True)
    async with AgentRuntime.open(state, [definition], observe_interval=0.05) as runtime:
        agent = runtime.agent("demo")
        assert agent.task(task_a.id).session_id == task_a.session_id
        assert len(agent.tasks()) == 3 and agent.memories()
        print("重启后仍可查询原 Task、Session 和记忆。", flush=True)


if __name__ == "__main__":
    with TemporaryDirectory(prefix="demoharness-super-agent-") as directory:
        asyncio.run(run(Path(directory)))
