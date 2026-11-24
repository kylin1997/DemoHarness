"""主动停止并续跑同一个 Task。运行 uv run python examples/resume_task.py。"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from demoharness import (
    AgentDefinition,
    AgentRuntime,
    FileCheck,
    RunReason,
    TaskState,
    Verdict,
)
from demoharness.core.loop import ModelRequest, ModelResponse, ModelStop

DRAFT = "# 巡检记录\n\n例行检查完成。\n"


async def run(root: Path) -> None:
    project = root / "project"
    project.mkdir()
    state = root / "state"
    ready_to_stop = asyncio.Event()

    async def first_model(request: ModelRequest, cancel: asyncio.Event) -> ModelResponse:
        if any(message.get("tool_call_id") == "write-draft" for message in request.messages):
            # 工具结果已经进入模型上下文，此时阻塞模型，等宿主主动停止。
            ready_to_stop.set()
            await cancel.wait()
            return ModelResponse(ModelStop.ABORTED, "")
        return ModelResponse(ModelStop.OK, "", [{
            "call_id": "write-draft",
            "name": "write_file",
            "params": {"path": "draft.md", "content": DRAFT},
        }])

    definition = AgentDefinition("demo", project, "scripted", first_model)
    async with AgentRuntime.open(state, [definition], observe=False) as runtime:
        agent = runtime.agent("demo")
        receipt = await agent.submit(
            "写入 draft.md 并核对巡检记录。",
            allowed_write_paths=["draft.md"],
            acceptance=[FileCheck("draft.md", equals=DRAFT)],
        )
        await asyncio.wait_for(ready_to_stop.wait(), timeout=10)
        stopped = await agent.stop(receipt.task_id)
        assert stopped.state is TaskState.STOPPED and stopped.delivery is None
        (first_run,) = agent.runs(stopped.id)
        (written,) = agent.actions(stopped.id)
        assert first_run.reason is RunReason.STOPPED
        assert written.run_id == first_run.id and written.attempts == 1
        assert written.result is not None and not written.result.is_error
        assert (stopped.workspace / "draft.md").read_bytes() == DRAFT.encode("utf-8")
        print(f"Task {stopped.id}，Session {stopped.session_id}")
        print(f"第一次 Run {first_run.id} 已主动停止，当时 draft.md 已写入一次。")

    async def resume_model(request: ModelRequest, _cancel: asyncio.Event) -> ModelResponse:
        (result,) = [message for message in request.messages if message["role"] == "tool"]
        assert result["tool_call_id"] == "write-draft" and result["content"] == written.result.content
        return ModelResponse(ModelStop.OK, "巡检记录已写入，可以验收。")

    definition = AgentDefinition("demo", project, "scripted", resume_model)
    async with AgentRuntime.open(state, [definition], observe=False) as runtime:
        agent = runtime.agent("demo")
        reopened = agent.task(stopped.id)
        runs_before_resume = agent.runs(reopened.id)
        assert reopened.state is TaskState.STOPPED and reopened.session_id == stopped.session_id
        assert runs_before_resume == [first_run] and agent.actions(reopened.id) == [written]
        print(f"重开后尚未调用 resume() · Task 状态 {reopened.state} · Run 数量 {len(runs_before_resume)}")
        await agent.resume(reopened.id)
        completed = await agent.wait(reopened.id)

        assert completed.state is TaskState.COMPLETED, completed.detail
        assert (completed.id, completed.session_id) == (stopped.id, stopped.session_id)
        assert completed.workspace == stopped.workspace
        assert (completed.workspace / "draft.md").read_bytes() == DRAFT.encode("utf-8")
        previous, second_run = agent.runs(completed.id)
        assert previous == first_run and second_run.id != first_run.id
        assert second_run.reason is RunReason.COMPLETED
        assert agent.actions(completed.id) == [written]

        history = agent.history(completed.id)
        (action_result,) = [entry for entry in history if entry.kind == "action_result"]
        (stop_entry,) = [entry for entry in history if entry.kind == "stopped"]
        (completion,) = [entry for entry in history if entry.kind == "completed"]
        assert action_result.run_id == stop_entry.run_id == first_run.id
        assert action_result.data["action_id"] == written.id and completion.run_id == second_run.id
        assert completed.delivery is not None and completed.delivery.checks[0].verdict is Verdict.PASSED

        print(f"重开后显式续跑，第二次 Run {second_run.id} 验收通过，Task 和 Session ID 沿用原值。")
        print("第二次 Run 复用了历史中的写入结果。draft.md 字节符合预期，写入仍只执行了一次。")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="demoharness-resume-") as directory:
        asyncio.run(run(Path(directory)))
