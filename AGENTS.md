# DemoHarness 开发规约

DemoHarness 面向初学者。概念、边界和运行机制以架构总览及可运行的快速体验为准。需求以用户最近确认的版本为准。每次改动都应让新人能从一个可观察结果追到负责该结果的代码。 [架构总览](docs/architecture.md) · [快速体验](docs/guide.md#chapter-02)

## 用 Poteto Mode 开发新需求

使用 pstack 的 `poteto-mode` 选择并执行开发流程。在 Codex 里加载 `$pstack:poteto-mode`。如果当前工作区使用本地技能，读取 `.agents/skills/poteto-mode/SKILL.md`，并按同目录的 `references/codex-tools.md` 使用对应工具。在 Claude Code 中可调用 `/poteto-mode`。新检出环境可按所用客户端的说明安装 pstack。 [pstack 安装与调用](https://github.com/michael-denyer/pstack-claude#install)

1. **确认目标和验收。** 先读用户需求、README 和对应实现，确认入口、可观察结果和失败行为。按任务类型阅读 Poteto Mode 的 Feature、Bug Fix 或 Refactoring playbook。
2. **先定接口和数据形状。** 按 Architect 明确状态属于谁、模块如何交互、哪些事件由谁发出。能用小原型验证的分歧先运行验证。可独立交付的改动可用隔离工作树分工，由主开发者集成接口。
3. **按可验证的小步实现。** 优先复用协议和工具入口，保持最小 Loop 清晰。每一步通过真实入口核对行为，再进入下一步。涉及说明、时序或教学场景时，同步更新指南和代码地图。
4. **用 Prove It Works 验收。** 覆盖正常路径以及相关的拒绝、持久化或恢复路径。界面改动在浏览器里实际操作；HTTP/SSE 读取实际响应；涉及 SQLite 时关闭后重开核对数据。独立复核按需求和仓库实际能力安排。最后按验收单汇总结果，整理为可审查的提交。

## 从哪里改

| 关注点 | 实现入口 |
| --- | --- |
| 本机装配、配置、HTTP 和 SSE | [Application](src/demoharness/application.py) |
| 运行阶段、ReAct／计划编排、上下文与委派 | [Harness](src/demoharness/harness.py) · [最小 Agent Loop](src/demoharness/agent_loop.py) |
| 工具参数、发现、权限与审批 | [工具目录](src/demoharness/toolbox.py) · [Guardrails](src/demoharness/guardrails.py) |
| 模型请求、会话持久化与回放 | [模型适配器](src/demoharness/models.py) · [Store](src/demoharness/state.py) |
| 三栏界面和可交互教学图 | [运行界面](src/demoharness/web/index.html) · [代码地图](docs/code-map/demoharness-arch/demoharness-arch.html) |

工具 Schema 按用户轮次发现。宿主在每次调用时检查可见工具、参数和批准状态。UI 用实际事件驱动展示。Token 统计注明估算值或提供商用量，地图的箭头、类名和章节对应实际代码。

仓库根目录的 `skills/` 提供 DemoHarness 运行时使用的示例技能。编辑后重启服务即可加载。 [Skills 示例](docs/guide.md#chapter-09)

## 常用验证

```sh
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check src/demoharness tests examples
uv run --locked demoharness serve
```

用「离线演示」档案走快速体验。改动模型传输时，用本地兼容接口检查工具 Schema、流式增量和错误处理。改动地图时，检查十站、时序放大、树图卡联动、章节点位和源码路径。 [快速体验](docs/guide.md#chapter-02)
