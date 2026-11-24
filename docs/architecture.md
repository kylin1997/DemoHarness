# DemoHarness 架构总览

DemoHarness 用一个 Python 进程托管多个 Agent。CLI 和 Python API 调用同一个运行时。SQLite 保存 Task、Session 记录和 Agent 记忆，工作副本保存实际产物。Agent 可以同时推进多件事，同一个 Task 同时最多由一个 Worker 推进。

在继续阅读前，可以先运行 `uv run python examples/verified_task.py`。这个例子创建 `report.md`，然后逐字核对文件内容。后面的流程都可以用它对照。 [文件示例](../examples/verified_task.py)

## 先认清几个抽象

| 概念 | 职责和归属 |
| --- | --- |
| Agent 定义 | `AgentDefinition` 保存稳定 ID、所属项目、身份提示、模型入口和工具权限。CLI 可以读取 Agent 目录下的 `SOUL.md` 和 `AGENTS.md` 作为提示。 |
| Agent 实例 | `runtime.agent(id)` 返回的操作入口，能接收消息、查 Task、停止和恢复工作。同一宿主内的 Task、工作目录、协调记录和长期记忆按 Agent ID 隔离。 |
| Agent 运行时 | `AgentRuntime` 打开状态目录，管理 SQLite、协调器、观察器和 Worker 的生命周期。关闭后停止推进，重开时恢复可继续的工作。 |
| Worker | 运行时为一个 Task 调度的进程内异步任务。它认领 Task、开启 Run，准备或复用工作副本，再推进执行循环。 |
| Task | 一件可独立推进的工作，拥有目标、文件验收条件、写入范围、预算、工作副本及 Plan。 |
| Session | 完成一件事留下的历史，包括用户输入、模型轮次、工具记录、Plan 修订和交付。每个 Task 对应一个 Session，共用 ID。 |
| Plan | Task 当前准备做的有序步骤。每步有稳定 ID、状态和证据引用。模型可以修订计划，每次修订有版本和原因。 |
| Run | Task 的一次执行尝试，记录起止时间、配置版本和结束原因。停止后继续同一个 Task 时会开始新的 Run。 |
| Context | 一次模型请求的输入，来自当前目标、验收、Plan、Session 片段和相关记忆。每轮重新组装。 |
| 子 Agent | 主 Task 临时委派的只读调查者。有独立上下文，按需读取父 Session，完成后交回结论与证据。 |
| 长期记忆 | Agent 跨 Task 使用的偏好、项目事实、约束和经验，附带来源。项目知识可以按项目限定召回。 |

## 一次工作怎样流动

```mermaid
flowchart TD
    Direct["Python submit：已知目标与验收"] --> Queued["Task 入队，新 Task 建立同 ID 的 Session"]
    Input["聊天消息或 Git 提交"] --> Router["协调器"]
    Router -->|创建| Queued
    Router -->|关联| Existing["补充写入已有 Session"]
    Router -->|回复或澄清| Notice["通知"]
    Existing -->|需要继续执行且满足条件| Queued
    Existing -->|正在执行时在动作边界读取| Model
    Queued --> Worker["Worker 认领，开启 Run"]
    Worker --> Workspace["准备或复用工作副本"]
    Workspace --> Model["从 Session 组装 Context，调用模型"]
    Model -->|提出工具操作| Tool["宿主授权、记录并执行"]
    Tool -->|结果写入 Session| Model
    Model -->|提出完成| Verify["在实际文件上验收"]
    Verify -->|可纠正且有预算| Model
    Verify -->|通过| Delivery["保存验收证据和差异"]
```

协调器决定消息归属，Worker 推进 Task，模型提出操作，宿主负责授权、执行和验收。这几段使用不同的输入和权限。子 Agent 与主 Worker 共用 `run_loop()`，由各自的角色配置决定可用上下文、工具和完成条件。协调、Worker 与临时委派的分工参考了 DeepSeek Super Agent 的领域模型。 [DeepSeek 领域模型](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/001-domain.md)

## 从现象追到源码

| 想理解的问题 | 起点 | 接着读 |
| --- | --- | --- |
| 命令如何变成调用 | [CLI](../src/demoharness/cli.py) · [运行时和 Agent API](../src/demoharness/runtime.py) | [任务执行](task-execution.md) |
| 模型、工具和验收怎样循环 | [执行循环](../src/demoharness/core/loop.py) · [文件工具](../src/demoharness/tools/files.py) | [任务执行](task-execution.md) |
| 状态记在哪里，重启怎样继续 | [TaskStore](../src/demoharness/core/storage.py) · [Session 投影](../src/demoharness/core/session.py) | [Session 与恢复](session-and-recovery.md) |
| 怎样处理多件事、委派并积累经验 | [消息路由](../src/demoharness/coordination.py) · [委派](../src/demoharness/tools/delegation.py) · [记忆](../src/demoharness/memory.py) · [Git 观察](../src/demoharness/discovery.py) | [Super Agent](super-agent.md) |

## 生产化还要考虑什么

当前实现适用于单机教学。投入生产前，还需要根据数据敏感度、运行规模和工具风险完善这些方向。

1. **权限与隔离**。明确用户和服务身份。为高风险工具设置审批、执行沙箱和网络访问策略。
2. **可靠执行**。定义重试与幂等边界，验证数据库备份、迁移和故障恢复。
3. **并发与容量**。按用户或租户隔离任务，设计队列调度、限流及资源配额。
4. **数据与隐私**。集中管理凭据，规定 Session 和记忆的访问、保留与删除策略。
5. **可观测性与运营**。关联 Task、Run 和工具日志，建立指标、追踪、告警和审计。
6. **模型质量与成本**。用实际任务持续评测，设置调用预算、超时和降级策略。
