# DemoHarness 开发规约

DemoHarness 是一个教学用 Agent 工程。能力边界以 [首版能力](docs/specs/agent-feature-scope.md) 为准。结构以 [架构说明](docs/architecture.md) 为准。让新人能从入口跟到唯一的 Task 执行循环，再追踪一个具体能力。

- 使用实际领域术语。Agent 是长期身份，Task 是一件事，Session 保存它的历史，Run 是一次执行尝试。保持状态只有一个写入责任方。
- 优先用标准库、简单数据结构和一个可复现的教学场景。功能设计要覆盖上下文、工具、约束、验证与纠正。
- 实际交付要有验收证据。未知副作用先核对，预算耗尽和用户停止时禁止启动新动作。模型提示词不能扩大宿主授权。
- 常规验证走真实 Python API 或 CLI，采用可控模型和临时文件。真实模型演示单独运行，说明凭据和费用。
- 每项功能同步更新相关规格和示例。示例写清输入、预期过程、完成证据及复现命令。

```sh
uv sync
uv run pytest -q
uv run python examples/verified_task.py
```

本地协作技能可从 `.agents/workflow.md` 进入，确认后的设计草稿见 `.scratch/agent-redesign/`。这些文件用于开发协作。项目代码和教程按实际可运行的能力更新。`references/` 中的参考项目仅供查阅。
