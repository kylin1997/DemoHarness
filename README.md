# DemoHarness

DemoHarness 用一个可运行的通用任务型 Agent 教你开发 Harness。Agent 接受任务，记录并调整这次任务的工作流，在已获授权的范围内自主推进。Harness 负责接收消息和事件、执行工具、保存过程，以及自动验收或提交人工验收。

项目在本机的单进程里运行，使用 SQLite 保存状态。它演示的是基础机制。计划用于实际业务时，先看[生产化还要考虑什么](docs/architecture.md#生产化还要考虑什么)。

## 开始运行

准备 Python 3.11+、Git 和 uv。在 Linux 或 macOS 的仓库根目录执行下列命令。首次安装依赖需要访问包源。 [安装 uv](https://docs.astral.sh/uv/getting-started/installation/)

```sh
uv sync --locked
demo_dir="$(mktemp -d)"
mkdir "$demo_dir/project"
uv run demoharness chat demo --demo --project "$demo_dir/project" --state-dir "$demo_dir/state"
```

教学 CLI 使用可控模型，无需 API 密钥。先输入 `整理上线简报`，再输入日期变更事件。用 `/plan` 查看同一个 Task 的清单怎样修订，最后验收新版交付。 [教学 CLI 操作步骤](examples/README.md#操作教学-cli)

## 接下来读什么

1. 看本机运行时怎样接收任务，以及 Agent、Task、Session、Plan 和 Run 各自负责什么。 [架构总览](docs/architecture.md)
2. 用 `report.md` 示例把 Session 记录对应到模型、工具和验收的代码。再练习接入自己的工具和验收器。 [任务如何执行和交付](docs/task-execution.md)
3. 看停止、重启和崩溃时，系统怎样从 Session 账本判断下一步。 [Session、上下文和恢复](docs/session-and-recovery.md)
4. 看同一个 Agent 怎样处理多个请求、委派临时子 Agent，从历史提炼记忆，并从 Git 提交发现新工作。 [Super Agent 如何组织工作](docs/super-agent.md)

[全部可运行示例和真实模型的 CLI 用法](examples/README.md)
