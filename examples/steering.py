"""follow-up 与 steering 示例（examples/steering.py）。

演示：连续对话（follow-up）——同一个会话再 run 一次，上下文自然接续。
真实模型。先 cp examples/.env.example examples/.env。

运行：`uv run python examples/steering.py`
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from _common import demo_runtime


def main():
    runtime = demo_runtime("steering")
    arch = runtime.bot("architect")

    r1 = arch.run("帮我想一个变量名，表示「用户是否已登录」")
    print(f"[第 1 轮] {r1.content}\n")

    # follow-up：同一会话再跑一轮，上一轮内容在上下文里
    r2 = arch.run("好，就按你说的来，顺便给一个 Python 用法示例", session_id=r1.session_id)
    print(f"[第 2 轮] {r2.content}\n")

    # 历史里能看到两轮输入都在这条会话上
    print("会话历史：")
    for e in runtime.history(r1.session_id):
        print(f"  [{e.kind}] {e.content[:60]}")

    runtime.close()


if __name__ == "__main__":
    main()