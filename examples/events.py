"""事件流示例（examples/events.py）。

演示 task.events()：异步运行任务，实时订阅 task_started / tool_call_started /
tool_result / task_finished 等事件（SDK 调用方的数据源）。真实模型。先 cp examples/.env.example examples/.env。

运行：`uv run python examples/events.py`
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from _common import demo_runtime


async def go():
    runtime = demo_runtime("events")
    arch = runtime.bot("architect")

    # 异步 start：提交立即返回 TaskHandle
    handle = arch.start("用 read_file 读 README.md，然后告诉我项目是做什么的")

    # 实时订阅事件
    print("事件流：")
    async for ev in handle.events():
        print(f"  {ev.type}: {ev.data}")
        if ev.type == "task_finished":
            break

    result = await handle.aresult()
    print(f"\n终态 {result.stop_reason.value}：{result.content}")
    runtime.close()


if __name__ == "__main__":
    asyncio.run(go())
