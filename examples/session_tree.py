"""会话树示例（examples/session_tree.py）。

演示 fork（从历史节点分叉）、reset（回到历史节点重新开始）、delete（级联删除）。
真实模型。先 cp examples/.env.example examples/.env。

运行：`uv run python examples/session_tree.py`
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from _common import demo_runtime


def main():
    runtime = demo_runtime("tree")
    arch = runtime.bot("architect")

    # 跑出一个有历史的会话
    r = arch.run("列三个 Python 内置的序列类型")
    sid = r.session_id
    entries = runtime.history(sid)
    print(f"会话 {sid} 有 {len(entries)} 条历史\n")

    if not entries:
        print("无历史，结束")
        runtime.close()
        return

    # fork：从第一条 user entry 处叉出新会话（拷贝前缀）
    fork_at = entries[0].id
    child = runtime.fork_session(sid, at_entry_id=fork_at)
    print(f"fork 出新会话 {child.id}（parent={child.parent_id}），前缀 {len(runtime.history(child.id))} 条")

    # reset：把原会话活跃叶移回某个历史节点
    to_entry = entries[0].id
    runtime.reset_session(sid, to_entry_id=to_entry)
    print(f"reset：会话 {sid} 重置到 {to_entry}")

    # delete：级联删除原会话（会连带删 fork 子树）
    runtime.delete_session(sid)
    remaining = [s.id for s in runtime.sessions()]
    print(f"delete 后剩余会话：{remaining}")

    runtime.close()


if __name__ == "__main__":
    main()