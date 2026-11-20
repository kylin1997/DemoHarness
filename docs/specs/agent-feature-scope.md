# DemoHarness Agent 能力范围

状态：已确认（记录自项目讨论，作为后续设计实现的依据）

## 第一阶段定位

DemoHarness 第一阶段是一个**完整的、教学目的的 Agent harness 实现**（harness = 上下文管理 + 工具接口 + 约束 + 验证 + 纠正；定义自李博杰《深入理解 AI Agent》，Agent = Model + Harness），以 **SDK 形态**交付（类似 pi 的 `createAgentSession`）：通过 Python API 创建 bot、提交任务、获取结果。它运行长程任务（多步工具调用、中断恢复、上下文延续），但**不是常驻服务**——gateway、cron、channel、交互式 CLI 都不在第一阶段（进程内 BotRuntime 装配一切，用完即走）：这些不在 harness 五要素之内，属于把 harness 部署成常驻服务/产品的生产工程细节，非 harness 本体。Web 界面不提供。

## 目标能力全景

一个完整的 Agent 需要覆盖以下能力域。这是项目的长期蓝图，按教学需要分阶段实现，未进入当前阶段的能力域不提前实现。「阶段」列标记该项的实现安排：第一阶段 = 已确认纳入第一阶段；未排期 = 暂不实现，需要时再排。

| 能力域 | 内容 | 阶段 | 参考实现 |
|--------|------|------|----------|
| bot | 用户交互的个体：独立 SOUL（身份）、AGENTS.md（行动指南）、skill/工具、记忆、工作区，多个 bot 并存且互相隔离；bot 可持续优化（改指南/身份/工具，下次任务验证）。bot 不是内核概念，是配置实体 + 存储 bot_id 命名空间隔离；BotRuntime 装配 bot 的一切 | 第一阶段 | hermes profile/bot 与 SOUL.md（个体形态参考，不做其独立库）；nanobot SOUL.md/USER.md + 多实例 |
| Agent 运行与生命周期 | 任务编排、运行、取消与结束。**运行前判断执行策略**（任务型定位）：ReAct（走一步判断）、Plan-and-Execute、Reflection、Multi-Agent（动态工作流形态）。四种策略均为编排扩展，不进内核；第一阶段只实现 ReAct（内核循环即默认形态），其余三策略留扩展位 | 第一阶段 | pi `packages/agent/src/agent-loop.ts`；nanobot `agent/loop.py`（通道面）+ `agent/runner.py`（模型面）；pi durable child tasks（Multi-Agent 编排参考） |
| Session/Context/Memory | 会话、上下文与记忆的读取、更新和延续 | 第一阶段 | pi `session-manager.ts` + compaction；nanobot `session/` + `agent/memory.py` |
| Tool/Skills/MCP | 能力发现、调用、结果回填与权限约束 | 第一阶段 | pi `packages/mcp`、tool_search（BM25 延迟加载）；nanobot `agent/tools/`（发现式注册表） |
| State/Workspace 执行体系 | 运行状态、工作区与实际执行边界 | 第一阶段 | pi `packages/durable`（Document/Commit）；nanobot `security/`（workspace_access、network） |
| 上下文压缩 | 上下文超限或手动触发时将旧消息摘要化，后续请求携带摘要继续；上下文工程的核心机制 | 第一阶段 | pi `packages/coding-agent/src/core/compaction/`（自动压缩、分支摘要、文件追踪）；nanobot 两级流程：Consolidator 摘要追加 `memory/history.jsonl` → Dream 消化为长期记忆 |
| 会话树 | session 支持树形结构：new（新建）、reset（模型只见重置点之后的条目）、fork（从历史节点分叉） | 第一阶段 | pi 会话格式（JSONL 树形，entry 带 `id`/`parentId`，`pi.reset` entry），见 `packages/coding-agent/docs/session-format.md` |
| Durable 执行 | 先提交存储再执行（每步 checkpoint），进程中断后从上一个 checkpoint 续跑，不重复已产生副作用的步骤 | 第一阶段 | pi `packages/durable`（Harness/Conversation/Commit/Task 状态机/Submission），README 的 Persist and Resume 章节 |
| 场景模拟 | 真实模型驱动的复杂教学场景（`scenarios/`，项目根）：每个场景是一个模拟现实任务的剧本，完成过程综合运用 DemoHarness 特性（任务循环、工具、压缩、恢复、会话树、bot），提供输入、预期过程、完成证据、复现步骤。只做真实模型演示，不做模拟数据的玩具示例；设计几个复杂场景即可，太简单的不需要 | 第一阶段（随特性逐步建成） | DemoHarness 自研（教学交付四件套的载体，AGENTS.md「功能不减配」的证据集合） |
| config | 全局配置。模型配置解析链（任务 > 会话 > 全局）的权威；模型配置 = 选模型 + 生成参数（如推理强度/thinking 开关这类 provider 生成参数属于此，字段等接入真正支持它的模型时补，属纯增量，不进内核） | 第一阶段 | pi settings；nanobot `config/schema.py`（camelCase/snake_case 兼容） |
| 定时/自动化任务 | cron 创建定时、间隔、一次性任务，结果回传到创建时的会话 | 未排期 | nanobot `nanobot/cron/`（CronService、CronJob/CronStore/CronSchedule、session_delivery 回传投递），工具入口 `agent/tools/cron.py`，文档 `docs/automations.md` |
| LLM Providers | 真实模型接入与 provider 适配。第一阶段 SDK 至少需要一个可用的模型接入（OpenAI 兼容即可），完整 provider 体系未排期 | 第一阶段（最小可用：OpenAI 兼容接入）；完整体系未排期 | pi `packages/ai`；nanobot `providers/`（registry 集中元数据 + OpenAI 兼容通用后端） |
| Channel/Gateway | 消息接入、路由、服务与回传；渠道接入不做 | 未排期 | nanobot `channels/`、`gateway/`、`bus/`；pi 多接口（interactive/print/JSON/RPC）复用同一 agent 机制 |
| Daemon 与 Heartbeat | 常驻运行管理、心跳及其控制边界 | 未排期 | nanobot gateway 常驻进程 + heartbeat 系统调度（docs/automations.md） |
| 插件扩展 | 扩展接入、生命周期与已确认的兼容边界 | 未排期 | pi extensions/packages（进程内加载）；nanobot Agent Plugin（workspace/plugins/） |
| 多 Agent | bot 间任务分派（接到任务可拆分后：自己执行 / 派给另一个 bot / 启动 subagent 临时执行）；subagent 的并行与隔离。全部通过扩展/策略机制实现，不进内核 | 第一阶段（作为扩展/策略机制实现，不进内核） | pi durable child tasks/task graph；nanobot subagent 工具（临时会话+受管任务）；hermes delegate 工具（child AIAgent） |
| 后台任务/异步执行 | 任务发起后不必等待：后台 subagent 独立会话中执行，完成后结果作为新输入回投父会话（follow-up 送达）；重启后存活（safe 工具可重放自动续跑）。观察侧为 Agent View：后台任务列表，各自是可观察的 background session。作为扩展机制实现，不进内核 | 第一阶段（作为扩展机制实现，不进内核） | pi detached 任务边界 + 持久 subagent（22-subagent-background 示例）；nanobot 后台 subagent + 完成通知路由（wait=false + TerminalResult 回投）；hermes detached results（回合间送达） |
| 可观测 | 脱敏事件、日志与状态，支持观察和控制实际运行 | 第一阶段（事件流已是内核机制，供 SDK/测试/教学示例消费；脱敏日志等按需补） | pi telemetry（span 体系）；nanobot `bus/runtime_events.py` |
| 自学习经验更新 | 经验、记忆、Skills 的候选版本，经人工批准具体版本才生效；记录来源、版本并可回滚，不训练模型、不改框架代码 | 未排期 | nanobot Dream/memory（分层记忆 + GitStore 版本化，方向可借鉴） |

第一阶段聚焦的核心场景（与 AGENTS.md 核心功能范围对应）：任务循环、任务状态与恢复、工具执行、执行边界、结果验证。上下文压缩、会话树、Durable 执行三项提供上下文延续、会话结构与执行模型支撑。SDK 形态意味着一切能力通过 Python API 暴露（BotRuntime）。

参考代码位置均相对 `references/pi`、`references/nanobot`、`references/hermes-agent`，三个项目均已建立 codegraph 索引，可用 `codegraph query/callers/callees` 直接定位。hermes-agent 代码量大，参考时只取与能力域对应的 agent 内核部分（运行循环、状态/会话、压缩、会话树、durable、工具分发协议），忽略具体工具与渠道适配实现。

## 未排期

以下能力明确暂不实现，记录于此避免重复讨论；启动前需先在本表更新「阶段」列。

- **自更新/发布系统**：nanobot 的 `update_*.py` 模块群（18 个）+ `release_feed.py`/`release_trust.py` + 更新协议（备份、回滚、重启交接）。产品级功能；属把 harness 部署成常驻服务/产品的生产工程细节，不在 harness 五要素之内，暂缓。
- **Agent 社交网络接入**：nanobot docs/agent-social-network.md，读取远端 `skill.md` 加入 agent 社区。超纲，暂缓。

以下属于现有能力域的实现细节或子特性，需要时归入对应能力域实现，不单独立项：

- 工具发现搜索 tool_search（→ Tool/Skills/MCP）
- 多 Agent 的工作流编排形态（→ 多 Agent，作为扩展；内核只需保证会话/任务挂载点足够）
- Code Mode 沙箱脚本执行（→ Tool 的高级形态）
- OpenAI 兼容 API 服务（→ Channel/Gateway 的一种）
- 提示模板、主题、键位、斜杠命令等终端交互（→ 交互配置）
- 遥测 Tracing span 体系（→ 可观测）
- child tasks / task graph（→ 多 Agent / Agent 生命周期）
- 会话存储 SQLite 化、事实与显示分离（→ State/Workspace）
- DM 配对授权 pairing（→ Channel 安全边界，接真实聊天渠道时再议）
