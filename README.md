# DemoHarness

DemoHarness 是一个 Python 教学项目，用来理解 Agent 调用模型和工具时，应用程序需要负责什么。你可以从一次消息开始，看到系统如何确定任务、保存上下文、限制工具权限、检查真实产物，以及在中断后继续。

项目在本机的单进程里运行，使用 SQLite 保存状态。它演示的是基础机制。计划用于实际业务时，先看[生产化还要考虑什么](docs/architecture.md#生产化还要考虑什么)。

## 开始运行

准备 Python 3.11+、Git 和 uv。文件工具使用 POSIX 接口，建议在 Linux 或 macOS 上运行。 [安装 uv](https://docs.astral.sh/uv/getting-started/installation/)

```sh
uv sync --locked
uv run python examples/verified_task.py
```

示例会在临时 Git 工作副本里写入 `report.md`。你会看到文件正文、`passed` 验收结果、SHA-256 和 Git 差异。Task 与 Session 的 ID 相同，源仓库保持原样。示例使用可控模型，无需 API 密钥。

## 接下来读什么

1. 先弄清 Agent、Task、Session、Plan 和 Run 各自负责什么，以及 CLI 和 Python API 怎样进入同一个运行时。 [架构总览](docs/architecture.md)
2. 跟着 `report.md` 例子走一遍，从创建任务到模型提议写文件，再到宿主验收并记录交付。 [任务如何执行和交付](docs/task-execution.md)
3. 看停止、重启和崩溃时，系统怎样从 Session 账本判断下一步。 [Session、上下文和恢复](docs/session-and-recovery.md)
4. 看同一个 Agent 怎样处理多个请求、委派临时子 Agent，从历史提炼记忆，并从 Git 提交发现新工作。 [Super Agent 如何组织工作](docs/super-agent.md)

[全部可运行示例和真实模型的 CLI 用法](examples/README.md)
