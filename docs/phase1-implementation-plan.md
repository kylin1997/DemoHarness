# DemoHarness 第一阶段实现规划

状态：已确认。实现顺序的依据：数据地基先行（一切依赖它）、API 形状先行（防返工）、循环放中段（前面四件拼装它）。每步的验收同时是教学交付四件套（输入、预期过程、完成证据、复现步骤）。

| 步 | 模块 | 内容 | 验收标准（测试 + 证据） |
|---|---|---|---|
| 0 | BotRuntime API 形状 | 定 SDK 门面：`build(config) / run / steering / cancel / 读状态` 的函数签名与返回类型（docs/architecture.md 的 SDK 形态） | 签名评审通过；类型能表达六态 stop_reason、Task 状态、entry 树查询 |
| 1 | core/storage.py + core/session.py | 数据地基：SQLite（entries/sessions 表、entry 树、六动词+守卫、BEGIN IMMEDIATE、会话 FIFO 串行化） | 单测：六动词语义、树重放算法、commit 原子性、忙碌守卫、id 单调 |
| 2 | core/task.py | Task 状态机（三相 checkpoint、六态 stop_reason）+ 恢复扫描（物化中断恰好一次） | 单测：phase 推进先落盘、中断物化、恢复后续跑语义 |
| 3 | core/tools.py + tools/ | 工具三段式协议、注册表（replay 字段）、首批 2-3 个教学工具（如读写文件） | 单测：三段式、参数校验、错误转 ToolResult 不抛异常 |
| 4 | core/loop.py + providers/ | ReAct 循环（stream_fn 注入点、steering/follow-up 注入、finish_turn 决策）+ OpenAI 兼容最小接入 | mock 模型单测：多轮工具循环、预算耗尽、取消、steering 注入；**最小 SDK 闭环可跑**（mock） |
| 5 | core/events.py + bots/ | 事件流（SDK/测试/教学示例消费的协议）+ bot spec（SOUL/AGENTS.md/工具白名单、bot_id 命名空间装配） | 单测：事件序列、bot 隔离（会话/工作区过滤） |
| 6 | compaction/ | 两段式压缩（阈值触发、切点选择、事务外摘要+事务内提交） | 单测：mock 摘要的压缩重放、边界切点规则、压缩后上下文重建 |
| 7 | 多 Agent（任务分派，扩展） | 三个去向：自己执行 / 派给另一个 bot（对方 spec 开会话）/ 启动 subagent（内联参数临时会话）。一机制两来源，不进内核；策略扩展提供 delegate_subagent/assign_bot 等工具 | 单测：分派三去向、subagent 会话隔离与结果回传、子父溯源链接 |
| 8 | 后台任务/异步执行（扩展） | 后台 subagent 不阻塞父会话，完成后 follow-up 回投；重启后存活（safe 工具可重放自动续跑、副作用工具物化中断）；Agent View 后台任务列表 | 单测：回投、重启存活重排、重放上限（3 次停车）、Agent View 查询 |
| 9 | scenarios/ | 首个复杂场景（真实模型，综合运用循环/工具/恢复/压缩/多 bot/后台任务） | 场景可复现（真实模型证据） |

> 可观测（事件流）不单列一步——事件流内核在第 5 步，场景与教学示例在第 9 步消费它，它横切各步而非新增模块。

原则（AGENTS.md 准则）：

- 每步一个教学能力聚焦，同步更新相关文档；测试用 mock 模型保持确定性，场景步骤用真实模型
- core/ 合计不超 1800 行参照值（模块预算表），突破时显式决策
- 第 4 步完成即有"最小闭环"里程碑（mock 模型跑通完整任务循环），适合做一次阶段性提交与演示
