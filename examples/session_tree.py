"""会话树示例（examples/session_tree.py）。

演示会话树动词：fork（从历史节点分叉出新会话）、reset（回到历史节点重新开始）、
delete（级联删除 fork 子树）。用 mock 模型。运行：`uv run python examples/session_tree.py`
"""

from __future__ import annotations

from pathlib import Path

from democlaw.core.loop import ModelResponse, ModelStop
from democlaw.runtime import BotRuntime, RuntimeConfig


def make_mock_stream_fn():
    state = {"n": 0}

    async def stream_fn(request, cancel):
        state["n"] += 1
        return ModelResponse(ModelStop.OK, f"回答 {state['n']}", [])

    return stream_fn


def main():
    runtime = BotRuntime.build(
        RuntimeConfig(db_path=str(Path.home() / ".democlaw" / "tree.db"), stream_fn=make_mock_stream_fn())
    )
    arch = runtime.bot("architect")

    # 1. 跑出一个会话，攒几条历史
    r = arch.run("做一件事")
    sid = r.session_id
    entries = runtime.history(sid)
    print(f"会话 {sid}，历史 {len(entries)} 条")

    # 2. fork：从第一条 entry 处叉出一个新会话（拷贝前缀到该点）
    fork_at = entries[0].id  # 第一条 user entry
    child = runtime.fork_session(sid, at_entry_id=fork_at)
    print(f"\nfork：从 {fork_at} 叉出新会话 {child.id}（parent={child.parent_id}）")
    print(f"  子会话历史 {len(runtime.history(child.id))} 条（拷贝前缀）")

    # 3. reset：把会话活跃叶移回历史节点
    #    取最后一条 entry 作为重置点，重置后模型只见其后内容（这里是「重置标记」起）
    last = entries[-1].id
    runtime.reset_session(sid, to_entry_id=last)
    print(f"\nreset：会话 {sid} 重置到 {last}")

    # 4. delete：级联删除 fork 子树（删除根会话会连带删 child）
    runtime.delete_session(sid)
    remaining = [s.id for s in runtime.sessions()]
    print(f"\ndelete 后剩余会话：{remaining}")
    print(f"  （根 {sid} 与 fork 子 {child.id} 都被级联删除）")

    runtime.close()


if __name__ == "__main__":
    main()