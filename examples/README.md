# DemoClaw 使用示例（examples/）

本目录存放**如何使用 DemoClaw SDK** 的最小可运行示例——面向刚上手的开发者，
展示「几分钟跑通」的最短路径（build → run → 读结果 → steering → 恢复）。

## 与 scenarios/ 的区别

- `examples/`：**API 上手**。短、纯 SDK、mock 模型即可跑，证明 SDK 自足。
- `scenarios/`（第 9 步）：**教学验证**。长、真实模型、综合多特性，每个场景
  证明一个特性的价值（附输入/预期过程/完成证据/复现步骤四件套）。

两者不重复：examples 教你「怎么调用」，scenarios 证明「为什么需要它」。

## 目录状态

**占位中**：SDK 门面（第 0 步 `BotRuntime`）尚未实现，当前 `src/democlaw/` 只有
内核层（storage/session/task/tools/loop/events），对外的一行式门面 API
（`BotRuntime.build()` / `bot.run()` / `task.steer()` 等）还未落地。

示例脚本将在此就位后填充，预计覆盖：

1. `quickstart.py` — 一行 build、一行 run、读结果（mock 模型）
2. `steering.py` — 运行中 steer + 结束后续聊 follow-up
3. `recovery.py` — 中断恢复（发现 interrupted 会话 → 显式续跑）
4. `session_tree.py` — fork/reset/delete 会话树动词

每个示例用 mock 模型保证确定性、不依赖网络与真实 API key，可直接 `uv run
python examples/xxx.py` 复现。