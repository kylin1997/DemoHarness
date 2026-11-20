"""多 bot 隔离示例（examples/bots.py）。

演示多个 bot 并存且互相隔离：各有自己的会话、记忆、行动指南与工具集，
按 bot_id 命名空间隔离。真实模型。先 cp examples/.env.example examples/.env。

运行：`uv run python examples/bots.py`
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from _common import demo_runtime

from demoharness.core.session import NewOp


def main():
    runtime = demo_runtime("bots")

    # 两个 bot 各自开会话（bot_id 命名空间隔离）
    async def open_sessions():
        a = await runtime._session_mgr.submit("", NewOp("", bot_id="architect"))
        w = await runtime._session_mgr.submit("", NewOp("", bot_id="writer"))
        return a.id, w.id

    a_sid, w_sid = asyncio.run(open_sessions())

    # 各自跑任务，结果沉淀在各自的世界里（不同 bot 的历史互不可见）
    ra = runtime.bot("architect").run("列一个微内核架构的设计要点", session_id=a_sid)
    print(f"architect（{a_sid}）回答：{ra.content[:80]}...")

    print(f"\n两个 bot 世界互相隔离：architect={a_sid}，writer={w_sid}")
    print("历史只按各自 bot_id 过滤，互不串扰；workSpace/记忆也按 bot 隔离。")

    runtime.close()


if __name__ == "__main__":
    main()