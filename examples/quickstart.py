"""DemoClaw 最小使用示例（examples/quickstart.py）。

真实模型跑通「build → run → 读结果 → 会话树」的最短路径。
先 cp examples/.env.example examples/.env 并填 OPENAI_API_KEY。

运行：`uv run python examples/quickstart.py`
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from _common import demo_runtime


def main():
    runtime = demo_runtime("quickstart")
    arch = runtime.bot("architect")

    # 1. 跑一个任务
    result = arch.run("读一下 README.md，告诉我它是做什么的")
    print(f"结果({result.stop_reason.value}): {result.content}")

    # 2. 看会话树
    for s in runtime.sessions():
        print(f"会话 {s.id} (bot={s.bot_id}) 活跃叶={s.active_entry_id}")

    # 3. 读历史
    for e in runtime.history(result.session_id):
        print(f"  [{e.kind}] {e.content[:60]}")

    runtime.close()


if __name__ == "__main__":
    main()