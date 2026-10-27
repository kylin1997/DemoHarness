"""steering 与 follow-up 示例（examples/steering.py）。

演示两类「运行中/结束后追加输入」：
- steering（运行中追加指令）：任务在工具调用之间插入一条指令，不打断进行中的工作
- follow-up（结束后排队的新输入）：任务结束后用同一会话再跑一轮，天然接续

用 mock 模型保证确定性。运行：`uv run python examples/steering.py`
"""

from __future__ import annotations

from pathlib import Path

from democlaw.core.loop import ModelResponse, ModelStop
from democlaw.runtime import BotRuntime, RuntimeConfig


def make_mock_stream_fn():
    """mock 模型：每次被问到就回一句短答，不带工具。"""
    state = {"n": 0}

    async def stream_fn(request, cancel):
        state["n"] += 1
        return ModelResponse(ModelStop.OK, f"这是第 {state['n']} 轮回答", [])

    return stream_fn


def main():
    runtime = BotRuntime.build(
        RuntimeConfig(db_path=str(Path.home() / ".democlaw" / "steering.db"), stream_fn=make_mock_stream_fn())
    )
    arch = runtime.bot("architect")

    # 第一次 run
    r1 = arch.run("第一步")
    print(f"[run 1] {r1.stop_reason.value}: {r1.content}")

    # follow-up：同一会话再跑一轮（会话延续），历史自然接续
    r2 = arch.run("接着上一步继续", session_id=r1.session_id)
    print(f"[run 2] {r2.stop_reason.value}: {r2.content}")

    # 看这段连续对话的历史：两个 user 输入都在这条会话上
    print(f"\n会话 {r1.session_id} 的历史：")
    for e in runtime.history(r1.session_id):
        print(f"  [{e.kind}] {e.content[:50]}")

    runtime.close()


if __name__ == "__main__":
    main()