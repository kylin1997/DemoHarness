"""多 Agent 分派示例（examples/subagent.py）。

演示 delegate_subagent 工具：agent 把一个聚焦子任务交给临时 subagent 执行，
result 回传（子会话不进父上下文，但执行记录可追）。真实模型。先 cp examples/.env.example examples/.env。

运行：`uv run python examples/subagent.py`
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from _common import demo_runtime


def main():
    runtime = demo_runtime("subagent")
    arch = runtime.bot("architect")

    task = (
        "用 delegate_subagent 工具派一个子任务：让 subagent 研究『Python 的 "
        "async/await 和线程的区别』，把它的回答转述给我。"
    )
    result = arch.run(task)
    print(f"结果：{result.content}\n")

    # 派生的 subagent 会话入过库，可从 sessions 看到（背景会话除外）
    print("所有会话：")
    for s in runtime.sessions():
        print(f"  {s.id} (bot={s.bot_id})")

    runtime.close()


if __name__ == "__main__":
    main()