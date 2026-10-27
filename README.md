# DemoClaw

DemoClaw 是一个**完整的、教学目的的 Agent harness 实现**（**Agent = Model + Harness**；harness = 上下文管理 + 工具接口 + 约束 + 验证 + 纠正，定义自李博杰《深入理解 AI Agent》）。先有教学目标，才追求"简单"——不必引入生产环境的工程细节；但 harness 本体完整，不减配。借鉴 pi 的实现，演示并提供一个可实际运行的长程任务 harness。核心只保留任务执行的最小闭环，其余能力（定时任务、上下文压缩、会话树、durable 执行等）按阶段以独立模块叠加在核心之上。优先保证流程清晰、行为可观察、示例可复现。

能力范围与阶段划分见 [docs/specs/agent-feature-scope.md](docs/specs/agent-feature-scope.md)。

## 快速开始

```sh
uv sync
uv run pytest          # 常规测试（mock 模型，确定性）
```

跑最小 SDK 示例（无需 API key）：

```sh
uv run python examples/quickstart.py
```

## 启动 Web 观察面

DemoClaw 提供一个 Web 仪表盘（会话列表 | 历史 | 实时事件流），构建在 SDK 之上的薄壳。

**方式一：mock 模型（无需 key，教学演示跑通界面）**

```sh
uv run python -m web --mock
```

打开 http://127.0.0.1:8765 ，即可发任务、看会话与运行过程。

**方式二：真实模型（OpenAI 兼容端点）**

```sh
export OPENAI_API_KEY=sk-...          # 必需
export OPENAI_BASE_URL=...            # 可选，OpenAI 兼容端点（默认官方）
export MODEL=gpt-4o                    # 可选，模型名（默认 gpt-4o）
uv run python -m web
```

模型配置链路：`OPENAI_API_KEY`（密钥，只经环境变量、不写源码）→ `OPENAI_BASE_URL`（端点）→ `MODEL`（模型名）。SDK 层另有三层模型解析（任务 > 会话 > 全局），详见 [docs/architecture.md](docs/architecture.md)「模型配置」节。

## 学习路径

1. 读 [docs/specs/agent-feature-scope.md](docs/specs/agent-feature-scope.md)：了解 DemoClaw 要演示哪些能力、分几步实现，以及每个能力域在参考项目中的对应实现。
2. 跑起来：

```sh
uv sync
uv run pytest
```

3. 读参考项目源码：`references/` 下有 pi（TypeScript，最小 agent harness）和 nanobot（Python agent 框架）的完整源码，spec 中每个能力域都标注了参考文件路径，可对照阅读。
4. 读 `docs/` 下的设计说明：`specs/` 是能力范围（做什么），`design/` 是各模块设计稿（怎么做），`architecture.md` 是整体结构。

## 目录

```text
DemoClaw/
├── AGENTS.md    # 项目开发规约（含新人上手指引）
├── docs/        # 设计说明与教学材料（specs/ 能力范围、design/ 模块设计稿）
├── src/         # DemoClaw 源码
├── bots/        # bot 定义（每 bot：SOUL.md + AGENTS.md + spec.json）
├── web/         # Web 观察面（SDK 之上的薄壳，python -m web 启动）
├── examples/    # 最小可运行 SDK 示例
└── references/  # 供学习对照的开源项目源码（git 忽略）
```
