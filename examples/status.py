"""任务/会话状态查询示例（examples/status.py）。

演示观察面的查询 API：sessions() / session_tasks() / task() / sessions_with()。
类似 Claude 的 show session detail。真实模型。先 cp examples/.env.example examples/.env。

运行：`uv run python examples/status.py`
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from _common import demo_runtime

from demoharness.core.task import StopReason


def main():
    runtime = demo_runtime("status")
    arch = runtime.bot("architect")

    # 跑一个任务，产生会话与任务记录
    r = arch.run("列出三个设计模式")
    print(f"跑了任务 {r.task_id}，会话 {r.session_id}\n")

    # sessions()：全部会话
    print("会话列表：")
    for s in runtime.sessions():
        print(f"  {s.id} bot={s.bot_id} 活跃叶={s.active_entry_id} 忙碌={s.running_task_id}")

    # session_tasks()：该会话的任务历史
    print(f"\n会话 {r.session_id} 的任务历史：")
    for t in runtime.session_tasks(r.session_id):
        print(f"  task={t.task_id} phase={t.phase} stop={t.stop_reason} 工具x{t.tools_used}")

    # sessions_with()：按终态筛选
    print(f"\nsessions_with(INTERRUPTED)：{len(runtime.sessions_with(StopReason.INTERRUPTED))} 个（本次应为 0）")

    runtime.close()


if __name__ == "__main__":
    main()