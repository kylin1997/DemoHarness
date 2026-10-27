"""中断恢复示例（examples/recovery.py）。

演示「任务未终态时中断 → 恢复扫描物化 interrupted → sessions_with(INTERRUPTED)
发现 → 显式续跑」。用 mock 模型。运行：`uv run python examples/recovery.py`
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from democlaw.core.loop import ModelResponse, ModelStop
from democlaw.core.session import AppendOp, NewOp
from democlaw.core.task import StopReason, scan_and_recover
from democlaw.runtime import BotRuntime, RuntimeConfig


def make_mock_stream_fn():
    async def stream_fn(request, cancel):
        return ModelResponse(ModelStop.OK, "续跑完成", [])

    return stream_fn


def main():
    runtime = BotRuntime.build(RuntimeConfig(db_path=str(Path.home() / ".democlaw" / "recovery.db"), stream_fn=make_mock_stream_fn()))

    # 构造一个「未终态」会话：append 一条 user entry（任务输入已落盘、但无终态）
    async def build_unterminated():
        info = await runtime._session_mgr.submit("", NewOp("", bot_id="architect"))
        await runtime._session_mgr.submit(info.id, AppendOp(info.id, "user", "一个没跑完的任务", parent=None))
        return info.id

    sid = asyncio.run(build_unterminated())
    print(f"构造会话 {sid}：任务输入已落盘、未终态（模拟进程在此刻被杀）")

    # 恢复扫描：物化 interrupted（模拟重启时 BotRuntime 的启动扫描）
    reports = scan_and_recover(runtime._storage, runtime._session_mgr)
    print(f"启动扫描：物化 {len(reports)} 个未终态会话")

    # 发现入口
    interrupted = runtime.sessions_with(StopReason.INTERRUPTED)
    print(f"sessions_with(INTERRUPTED)：{len(interrupted)} 个中断会话")

    # 显式续跑：用同一会话重新提交（架构「恢复」语义：续跑由调用方显式重新提交）
    # 注意：这里演示发现入口即可，实际续跑是 bot.run("继续", session_id=sid)
    print(f"\n中断会话已可发现。续跑 = bot.run('继续刚才的任务', session_id='{sid}')")

    runtime.close()


if __name__ == "__main__":
    main()