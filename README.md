# DemoClaw

DemoClaw 是一个教学用的 Agent 示例项目，借鉴 OpenClaw 与 pi 的思路，演示 Agent 如何执行长程任务。核心只保留任务执行的最小闭环，其余能力（定时任务、上下文压缩、会话树、durable 执行等）按阶段以独立模块叠加在核心之上。优先保证流程清晰、行为可观察、示例可复现。

能力范围与阶段划分见 [docs/specs/agent-feature-scope.md](docs/specs/agent-feature-scope.md)。当前已完成最小工程骨架，功能尚未实现。

## 学习路径

1. 读 [docs/specs/agent-feature-scope.md](docs/specs/agent-feature-scope.md)：了解 DemoClaw 要演示哪些能力、分几步实现，以及每个能力域在参考项目中的对应实现。
2. 跑起来：

```sh
uv sync
uv run pytest
```

3. 读参考项目源码：`references/` 下有 pi（TypeScript，最小 agent harness）和 nanobot（Python agent 框架）的完整源码，spec 中每个能力域都标注了参考文件路径，可对照阅读。
4. 随实现推进阅读 `docs/` 下的设计说明：每个教学能力落地时会有对应的设计文档和可复现示例。

## 目录

```text
DemoClaw/
├── AGENTS.md    # 项目开发规约（含新人上手指引）
├── docs/        # 设计说明与教学材料
├── src/         # DemoClaw 源码
└── references/  # 供学习对照的开源项目源码（git 忽略）
```
