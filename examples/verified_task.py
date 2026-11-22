"""Run a verified file task in a temporary copy of a Git project."""

import argparse
import asyncio
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from demoharness import AgentDefinition, AgentRuntime, FileCheck, TaskState
from demoharness.core.loop import ModelResponse, ModelStop
from demoharness.providers.openai import make_openai_stream_fn


CONTENT = "# DemoHarness result\n\nThe file check passed.\n"


async def scripted_model(request, cancel):
    if any(message["role"] == "tool" for message in request.messages):
        return ModelResponse(ModelStop.OK, "已写入报告。")
    return ModelResponse(ModelStop.OK, "", [{
        "call_id": "report-1",
        "name": "write_file",
        "params": {"path": "report.md", "content": CONTENT},
    }])


async def run(root: Path, live: bool):
    project = root / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "README.md").write_text("# Teaching project\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "README.md"], check=True)
    subprocess.run([
        "git", "-C", str(project), "-c", "user.name=DemoHarness", "-c", "user.email=demo@example.invalid",
        "commit", "-qm", "Initial teaching fixture",
    ], check=True)

    model = "scripted"
    complete = scripted_model
    if live:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise SystemExit("真实模型需要 OPENAI_API_KEY。")
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get("MODEL", "gpt-4o")
        complete = make_openai_stream_fn(key, base_url)

    definition = AgentDefinition("demo", project, model, complete)
    async with AgentRuntime.open(root / "state", [definition], observe=False) as runtime:
        agent = runtime.agent("demo")
        receipt = await agent.submit(
            f"创建 report.md，内容必须逐字等于 {CONTENT!r}。",
            acceptance=[FileCheck("report.md", equals=CONTENT)],
        )
        task = await agent.wait(receipt.task_id)
        if task.state is not TaskState.COMPLETED:
            raise SystemExit(f"验收失败：{task.state} {task.detail}")
        print(f"Task {task.id} 已完成，Session {task.session_id}")
        print((task.workspace / "report.md").read_text(encoding="utf-8"), end="")
        print(f"验收与差异：{task.delivery}")
        assert not (project / "report.md").exists(), "原始仓库不应被修改"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="使用配置的 OpenAI 兼容模型")
    with TemporaryDirectory(prefix="demoharness-task-") as directory:
        asyncio.run(run(Path(directory), parser.parse_args().live))
