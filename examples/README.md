# 跑一个教学场景

| 场景 | 命令 | 应看到的结果 |
|---|---|---|
| 验证文件任务 | `uv run python examples/verified_task.py` | 临时 Git 副本生成 `report.md`，打印检查与差异。 |
| 综合场景 | `uv run python examples/super_agent_demo.py` | 先交付 B，再修订 A 并委派子 Agent，随后查看记忆与主动创建的 Task。重启后原 Session 可以查询。 |
| 同时处理两件事 | `uv run pytest -q tests/test_agent_coordination.py` | A 等待时 B 完成，A 的补充改动生效。同一 Task 可在补齐验收后继续。 |
| 临时子 Agent | `uv run pytest -q tests/test_agent_delegation.py` | 父子拥有独立上下文，预算和中断记录可核对。 |
| 记忆与主动发现 | `uv run pytest -q tests/test_super_agent.py tests/test_agent_memory.py` | 验收证据被提炼和召回，本地新提交可触发新工作，重复提交不重复建 Task。 |
| 崩溃恢复 | `uv run pytest -q tests/test_agent_runtime.py -k crash` | `SIGKILL` 后对账、复用结果或等待，撤销的写入不重放。 |

真实模型入口见项目 [README](../README.md)。其余早期示例使用 `legacy_runtime.py`，用于对照原 SDK。
