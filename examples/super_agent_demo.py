"""Observe two Tasks, a delegated investigation, memory, and a Git discovery."""

import asyncio
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from demoharness import (
    Agent,
    AgentDefinition,
    AgentRuntime,
    FileCheck,
    Input,
    TaskState,
)
from demoharness.core.loop import ModelResponse, ModelStop

A_GOAL = "工作 A：写 a.txt"
A_REQUEST = "授权写入：a.txt | 工作 A，请写入一行「旧要求」并核对全文。"
A_FOLLOW_UP = "把工作 A 的 a.txt 改成一行「新要求」，并按新内容验收。"
A_ORIGINAL = "旧要求\n"
A_UPDATED = "新要求\n"


async def send_and_wait_for_route(agent: Agent, text: str) -> Input:
    message_id = await agent.send(text)
    async with asyncio.timeout(8):
        while True:
            try:
                return agent.input(message_id)
            except KeyError:
                await asyncio.sleep(0.02)


async def until(lookup):
    async with asyncio.timeout(8):
        while (value := lookup()) is None:
            await asyncio.sleep(0.02)
        return value


def tool(name, **params):
    return ModelResponse(ModelStop.OK, "", [{"call_id": name, "name": name, "params": params}])


class DemoModel:
    def __init__(self):
        self.baseline = asyncio.Event()
        self.started_a = asyncio.Event()
        self.release_a = asyncio.Event()

    def route(self, context):
        text = context["text"]
        if context["kind"] == "project_observation":
            self.baseline.set()
            if "TODO" in text:
                assert any("a.txt" in memory for memory in context["memories"])
                return {"kind": "create", "text": "检查新提交中的待办", "work_key": "notes:todo",
                        "checks": [{"path": "notes.md", "contains": "TODO"}]}
            return {"kind": "skip"}
        if text == A_REQUEST:
            return {"kind": "create", "text": A_GOAL, "checks": [{"path": "a.txt", "equals": A_ORIGINAL}]}
        return {"kind": "clarify", "text": "请说明这条消息属于哪件事。"}

    async def __call__(self, request, cancel):
        prompt = request.system_prompt
        results = {m.get("tool_call_id") for m in request.messages if m["role"] == "tool"}
        if "你负责判断消息属于哪件事" in prompt:
            context = json.loads(request.messages[-1]["content"])
            decision = self.route(context)
            return ModelResponse(ModelStop.OK, json.dumps(decision, ensure_ascii=False))
        if "delegated request" in prompt:
            if "read_parent_history" not in results:
                return tool("read_parent_history", limit=20)
            assert any(A_FOLLOW_UP in m["content"] for m in request.messages if m["role"] == "tool")
            return ModelResponse(ModelStop.OK, "已从父 Session 核对补充要求。")
        if f"Current Task goal: {A_GOAL}\n" in prompt:
            if not any(m["role"] == "user" and m["content"] == A_FOLLOW_UP for m in request.messages):
                self.started_a.set()
                await self.release_a.wait()
                return tool("write_file", path="a.txt", content=A_ORIGINAL)
            if "set_plan" not in results:
                return tool("set_plan", expected_revision=1, reason="用户补充了新要求",
                            steps=["核对补充", "写入并验证 a.txt"])
            if "delegate" not in results:
                return tool("delegate", prompt="核对父 Session 的新要求")
            if "write_file" not in results:
                return tool("write_file", path="a.txt", content=A_UPDATED)
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
        first_message = await send_and_wait_for_route(agent, A_REQUEST)
        initial_a = agent.task(first_message.task_id)
        assert initial_a.id == initial_a.session_id
        assert initial_a.allowed_write_paths == {"a.txt"}
        print(f"协调器创建 A · 消息 {first_message.id} · Task {initial_a.id} · Session {initial_a.session_id}")
        await asyncio.wait_for(model.started_a.wait(), 8)
        b = await agent.submit("工作 B：写 b.txt", acceptance=[FileCheck("b.txt", equals="独立交付\n")],
                               allowed_write_paths=["b.txt"])
        task_b = await asyncio.wait_for(agent.wait(b.task_id), 8)
        assert task_b.state is TaskState.COMPLETED
        print(f"B 已交付 · Task {task_b.id}。此时 A 仍在等待模型。")
        follow_up_id = await agent.send(A_FOLLOW_UP, task_id=initial_a.id,
                                        acceptance=[FileCheck("a.txt", equals=A_UPDATED)])
        follow_up = agent.input(follow_up_id)
        assert follow_up.task_id == initial_a.id
        follow_up_entry = next(entry for entry in agent.history(initial_a.id)
                               if entry.kind == "input" and entry.data["input_id"] == follow_up.id)
        print(f"用户更新验收并关联补充消息 {follow_up.id} · Task {follow_up.task_id} · "
              f"Session {initial_a.session_id} 条目 {follow_up_entry.id}")
        model.release_a.set()
        task_a = await asyncio.wait_for(agent.wait(initial_a.id), 8)
        assert task_a.state is TaskState.COMPLETED, task_a.detail
        assert task_a.plan.revision == initial_a.plan.revision + 1
        assert (task_a.workspace / "a.txt").read_bytes() == A_UPDATED.encode("utf-8")
        history = agent.history(task_a.id)
        (revised,) = [entry for entry in history if entry.kind == "plan_revised"]
        (stale,) = [entry for entry in history if entry.kind == "stale_response"]
        print(f"A 的 Plan 版本 {initial_a.plan.revision} → {task_a.plan.revision} · 原因 {revised.data['reason']}")
        actions = agent.actions(task_a.id)
        (written,) = [action for action in actions if action.call.name == "write_file"]
        assert written.attempts == 1
        print(f"A 丢弃过时响应（Session 条目 {stale.id}），实际写入 {written.attempts} 次，验收通过。")

        (delegated,) = [action for action in actions if action.call.name == "delegate"]
        assert delegated.result and not delegated.result.is_error
        details = delegated.result.details
        (evidence,) = details["evidence"]
        assert evidence["tool"] == "read_parent_history" and follow_up_entry.id in evidence["entry_ids"]
        print(f"子 Agent 委派 {details['delegation_id']} · 父 Session 补充条目 {follow_up_entry.id} · {details['summary']}")

        (completion,) = [entry for entry in history if entry.kind == "completed"]
        delivery_source = f"task:{task_a.id}:entry:{completion.id}"
        memory = await until(lambda: next((item for item in agent.memories() if delivery_source in item.sources), None))
        print(f"Agent 记忆 {memory.id} · {memory.text} · 来源 {'、'.join(memory.sources)}")
        commit(project, "TODO: 核对 a.txt 的验收方法\n")
        known_tasks = {task_a.id, task_b.id}
        discovered = await until(lambda: next((task for task in agent.tasks() if task.id not in known_tasks), None))
        assert (await asyncio.wait_for(agent.wait(discovered.id), 8)).state is TaskState.COMPLETED
        print(f"新提交创建 Task {discovered.id} · {discovered.goal} · 已召回 a.txt 的验收记忆")
    async with AgentRuntime.open(state, [definition], observe_interval=0.05) as runtime:
        agent = runtime.agent("demo")
        restored = agent.task(task_a.id)
        assert restored.session_id == task_a.session_id and restored.plan == task_a.plan
        assert len(agent.tasks()) == 3 and any(item.id == memory.id for item in agent.memories())
        print(f"重开后仍可查询 A · Task {restored.id} · Session {restored.session_id} · 记忆 {memory.id}")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="demoharness-super-agent-") as directory:
        asyncio.run(run(Path(directory)))
