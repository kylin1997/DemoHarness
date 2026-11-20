"""中断恢复示例（examples/recovery.py）。

演示「任务未终态时中断 → 恢复扫描物化 interrupted → sessions_with(INTERRUPTED)
发现 → 显式续跑」。真实模型。先 cp examples/.env.example examples/.env。

运行：`uv run python examples/recovery.py`
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from _common import demo_runtime

from demoharness.core.session import AppendOp, NewOp
from demoharness.core.task import StopReason, scan_and_recover


def main():
    runtime = demo_runtime("recovery")

    # 构造「未终态」会话：append 一条 user entry（任务输入已落盘、无终态）
    async def build_unterminated():
        info = await runtime._session_mgr.submit("", NewOp("", bot_id="architect"))
        await runtime._session_mgr.submit(info.id, AppendOp(info.id, "user", "一个没跑完的任务", parent=None))
        return info.id

    sid = asyncio.run(build_unterminated())
    print(f"构造会话 {sid}：任务输入已落盘、未终态（模拟进程被杀）")

    # 恢复扫描（模拟重启时 BotRuntime 启动扫描）
    reports = scan_and_recover(runtime._storage, runtime._session_mgr)
    print(f"启动扫描：物化 {len(reports)} 个未终态会话")

    # 发现入口
    interrupted = runtime.sessions_with(StopReason.INTERRUPTED)
    print(f"sessions_with(INTERRUPTED)：{len(interrupted)} 个")

    # 显式续跑（真实模型续跑那条任务）
    print(f"\n续跑 = arch.run('继续刚才的任务', session_id='{sid}')")
    result = runtime.bot("architect").run("继续刚才没跑完的任务", session_id=sid)
    print(f"续跑结果：{result.content}")

    runtime.close()


if __name__ == "__main__":
    main()