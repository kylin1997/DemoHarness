"""DemoClaw 最小使用示例（examples/quickstart.py）。

用 mock 模型跑通「build → run → 读结果 → 会话树」的最短路径。不需要 API key。

运行：`uv run python examples/quickstart.py`
"""


from pathlib import Path

from democlaw.core.loop import ModelResponse, ModelStop
from democlaw.runtime import BotRuntime, RuntimeConfig


def make_mock_stream_fn():
    """一个确定性的 mock 模型：先调一次 read_file，再回最终答案。"""
    state = {"n": 0}

    async def stream_fn(request, cancel):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(ModelStop.OK, "我先读一下文件", [{"call_id": "c1", "name": "read_file", "params": {"path": "README.md"}}])
        return ModelResponse(ModelStop.OK, "已读完：这是一个教学用 agent harness。", [])

    return stream_fn


def main():
    runtime = BotRuntime.build(
        RuntimeConfig(db_path=str(Path.home() / ".democlaw" / "quickstart.db"), stream_fn=make_mock_stream_fn())
    )
    architect = runtime.bot("architect")

    # 1. 跑一个任务（同步阻塞）
    result = architect.run("读一下 README")
    print(f"结果({result.stop_reason.value}): {result.content}")

    # 2. 看会话树
    for s in runtime.sessions():
        print(f"会话 {s.id} (bot={s.bot_id}) 活跃叶={s.active_entry_id}")

    # 3. 读历史
    sid = result.session_id
    for e in runtime.history(sid):
        print(f"  [{e.kind}] {e.content[:40]}")

    # 4. 用完即走
    runtime.close()


if __name__ == "__main__":
    main()