# DemoHarness

DemoHarness 是一个 Python 教学项目。你可以观察一个 Agent 怎样用工具完成工作，怎样检查自己的产物，以及怎样在中断后继续。每件事有自己的 Task、Session、Plan 和工作副本。

## 十分钟上手

准备 Python 3.11+、Git 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。文件工具使用 POSIX 接口，建议在 Linux 或 macOS 上运行。

```sh
uv sync
uv run python examples/verified_task.py
```

示例把「创建 `report.md` 并逐字核对内容」交给可控模型。你会看到正文、`passed` 检查结果、SHA-256 和 Git 差异。示例使用临时仓库，无需模型凭据。

接下来按这个顺序读：

1. 看[系统设计](docs/architecture.md)，理解 Task、Session 和 Run，跟着架构图追踪这次写入的执行循环和验收器。
2. 想看多个 Task、临时子 Agent 和长期记忆怎样一起工作，运行综合示例：

   ```sh
   uv run python examples/super_agent_demo.py
   ```

   你会看到 B 先交付，A 收到补充后修改 Plan，随后有新的 Git 提交触发工作。示例最后关闭再打开运行时，检查已保存的状态。想观察写入前后突然退出时怎样处理，跑[崩溃恢复测试](examples/README.md#观察崩溃恢复)。

## 进阶用法

设置 `OPENAI_API_KEY` 后，可以用真实模型聊天。使用 OpenAI 兼容服务时，可设置 `OPENAI_BASE_URL` 和 `MODEL`。

```sh
uv run demoharness chat local --project /path/to/a/git/repository
```

[指令、多 Agent 配置和验证命令](examples/README.md)
