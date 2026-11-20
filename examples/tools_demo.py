"""工具三段式示例（examples/tools_demo.py）。

演示 agent 调用 read_file / write_file / run_command 三个内置工具，观察
工具调用如何落到 entry、结果如何回填。真实模型。先 cp examples/.env.example examples/.env。

运行：`uv run python examples/tools_demo.py`
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from _common import demo_runtime


def main():
    runtime = demo_runtime("tools")
    arch = runtime.bot("architect")

    # 让 agent 实际调用工具：读 ws 看内容、写一个文件、跑一条命令
    task = (
        "请用工具做三件事："
        "1) 读 README.md 的第一行，告诉我项目名；"
        "2) 写一个文件 _data/hello.txt，内容为 'Hello DemoHarness'；"
        "3) 运行 `ls examples` 并把输出告诉我。"
    )
    result = arch.run(task)
    print(f"结果：{result.content}\n")

    # 工具调用过程已落盘为 tool_call / tool_result entry
    print("工具调用历史：")
    for e in runtime.history(result.session_id):
        if e.kind in ("tool_call", "tool_result"):
            print(f"  [{e.kind}] {e.content[:80]}")

    runtime.close()


if __name__ == "__main__":
    main()