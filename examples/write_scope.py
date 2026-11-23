"""用临时项目演示写入授权。运行 uv run python examples/write_scope.py。"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from demoharness import AgentDefinition, AgentRuntime, FileCheck, TaskState, Verdict
from demoharness.core.loop import ModelRequest, ModelResponse, ModelStop

REPORT = "# 验收报告\n\n检查通过。\n"
NOTES = "原有项目笔记。\n"


async def scripted_model(request: ModelRequest, _cancel: asyncio.Event) -> ModelResponse:
    if any(message["role"] == "tool" for message in request.messages):
        return ModelResponse(ModelStop.OK, "已写入报告，项目笔记保持原样。")

    # 模拟模型请求写入两个文件，由宿主判定每个请求是否获准。
    return ModelResponse(ModelStop.OK, "", [
        {"call_id": "report", "name": "write_file", "params": {"path": "report.md", "content": REPORT}},
        {"call_id": "notes", "name": "write_file", "params": {"path": "NOTES.md", "content": "改写笔记\n"}},
    ])


async def run(root: Path) -> None:
    project = root / "project"
    project.mkdir()
    (project / "NOTES.md").write_bytes(NOTES.encode("utf-8"))
    state = root / "state"
    definition = AgentDefinition("demo", project, "scripted", scripted_model)

    async with AgentRuntime.open(state, [definition], observe=False) as runtime:
        agent = runtime.agent("demo")
        receipt = await agent.submit(
            "创建 report.md，内容为验收报告，并核对原有项目笔记。",
            allowed_write_paths=["report.md"],
            acceptance=[FileCheck("report.md", equals=REPORT), FileCheck("NOTES.md", equals=NOTES)],
        )
        completed = await agent.wait(receipt.task_id)
        assert completed.state is TaskState.COMPLETED, completed.detail

    # 从同一个状态目录重开，读取已持久化的动作结果和交付证据。
    async with AgentRuntime.open(state, [definition], observe=False) as runtime:
        agent = runtime.agent("demo")
        task = agent.task(receipt.task_id)
        assert task.allowed_write_paths == {"report.md"}
        assert (task.workspace / "report.md").read_bytes() == REPORT.encode("utf-8")
        assert (task.workspace / "NOTES.md").read_bytes() == NOTES.encode("utf-8")
        assert not (project / "report.md").exists()
        assert (project / "NOTES.md").read_bytes() == NOTES.encode("utf-8")

        actions = {action.call.call_id: action for action in agent.actions(task.id)}
        written, rejected = actions["report"], actions["notes"]
        assert written.result is not None and not written.result.is_error and written.attempts == 1
        assert rejected.result is not None and rejected.result.is_error and rejected.attempts == 0
        assert "outside the Task write scope" in rejected.result.content

        delivery = task.delivery
        assert delivery is not None and delivery == completed.delivery
        assert {check.path: check.verdict for check in delivery.checks} == {
            "report.md": Verdict.PASSED,
            "NOTES.md": Verdict.PASSED,
        }
        assert "report.md" in delivery.diff and "NOTES.md" not in delivery.diff

        print(f"report.md 写入成功，执行次数 {written.attempts}")
        print(f"NOTES.md 超出写入授权，宿主拒绝执行，执行次数 {rejected.attempts}，原始字节保持不变")
        print("重开运行时后读到两条动作记录。两项文件验收通过，交付差异仅包含 report.md。")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="demoharness-scope-") as directory:
        asyncio.run(run(Path(directory)))
