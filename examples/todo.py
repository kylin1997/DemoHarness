"""todo 工具示例（examples/todo.py）。

演示 agent 用 todo 工具维护待办清单（list/add/toggle/clear），状态存进
tool result 的 details（落盘进 entry，分支正确）。真实模型。先 cp examples/.env.example examples/.env。

运行：`uv run python examples/todo.py`
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from _common import demo_runtime


def main():
    runtime = demo_runtime("todo")
    arch = runtime.bot("architect")

    task = (
        "用 todo 工具规划「写完一篇 README」这个任务，列出 3 步，"
        "然后把第 1 步标记为完成，最后列出当前清单。"
    )
    result = arch.run(task)
    print(f"结果：{result.content}\n")

    # todo 状态落进 tool_result entry 的 details
    print("todo 相关工具调用：")
    for e in runtime.history(result.session_id):
        if e.kind == "tool_result" and "todo" in e.content:
            print(f"  {e.content[:120]}")

    runtime.close()


if __name__ == "__main__":
    main()