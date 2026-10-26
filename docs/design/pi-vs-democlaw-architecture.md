# DemoClaw 与 pi 的架构差异

本文对比 DemoClaw 的架构设计（`docs/architecture.md`）与 pi 参考实现（`references/pi`），供判断 DemoClaw 的架构设计是否成立。对比基于 pi 实际源码，非读文档印象：`packages/agent`（循环层）与 `packages/durable`（durable 存储层）是本对比的主战场。

定位依据以 `AGENTS.md` 为准：DemoClaw 是**完整的、教学目的的 Agent harness 实现**（Agent = Model + Harness；harness = 上下文管理 + 工具接口 + 约束 + 验证 + 纠正，定义自李博杰《深入理解 AI Agent》）。本文为「pi / nanobot / hermes 三者实现有差异时，差异如何取舍」提供判断依据——取舍本身由使用者判断，本文不是定论。

## 结论先行

DemoClaw 的架构设计与 pi 在 **harness 本体**上同构——两者对上下文管理、工具接口、约束、验证、纠正五要素的落地高度一致，因为它们面对的是同一套长程 Agent 的必然问题。真正的差异集中在一条根因上：**pi 是「harness 本体 + 生产工程全量」，DemoClaw 是「harness 本体不减 + 生产工程全减」**。

pi 为「可交付、可定制、可跑在多种环境」付出了大量 DemoClaw 没有的生产工程抽象（三后端 Storage、通用 Task 状态机、幂等提交、可分发插件生态、无服务器可恢复）。DemoClaw 把这些**刻意减掉**，因为它要交付的是「能被新人读懂、能被复现的 harness」，不是「能被别人安装定制的产品」。减掉的是 harness 五要素之外的生产工程细节，不触碰五要素本身——这正是 `AGENTS.md`「harness 完整性」条款的判据（harness 本体不减；减多进程并发、跨进程锁、回滚升级、规模化部署、gateway/cron/channel/交互式 CLI/插件市场）。判定：**DemoClaw 减对了，没减错。**

## 一致的部分（harness 五要素同构）

### 1. 循环形态：都是 ReAct，同一套停止契约

pi 的 `agent-loop.ts`（959 行）就是 DemoClaw loop.py 的骨：

- **`stream_fn`**：pi 的函数签名就是 `streamFn`，与 DemoClaw 的注入点逐字同名。pi 所有失败都编码进返回的 `message.stopReason`（`agent-loop.ts:254` 检查 `error`/`aborted`），与 DemoClaw「stream_fn 不得抛异常，失败编码为 stop_reason」是同一条契约。
- **补充输入分两类**：pi 的 `getSteeringMessages`（运行中追加）与 `getFollowUpMessages`（结束后排队）就是 DemoClaw 的 steering / follow-up，连「在当前工具调用之间插入、不打断进行中的 turn」的语义都一样——但 pi 的实现更精细（`pendingMessages` 只在新 turn 开始时取一次，避免一次处理两条）。
- **`finish_turn` 注入点**：pi 的 `AgentLoopConfig.finishTurn`（`types.ts:264`）每轮结束决定 `{ end, continue }`，与 DemoClaw 的 `finish_turn` 注入点语义完全一致。结果验证消费证据、不信模型自称，两者同此——这是 harness 五要素里「验证」的落点。
- **`before/after_tool_call` 三段式**：pi 的 `beforeToolCall`（可拦截 `block`）、`afterToolCall`（可改写 result）、`prepareArguments`（参数预处理）对应 DemoClaw 的 prepare → execute → finalize 三段式——这是「约束」与「纠正」的落点（工具调用边界 + 失败改写）。pi 额外做了：并行工具调用（`sequential`/`parallel` 两种执行模式）、`length` stop 时整批工具调用判失败（防截断参数）、`terminate` 字段传播。DemoClaw 第一阶段把这些归为扩展，无需内置。

### 2. 数据形态：entry 树 + head/reset/compact + replay，同构

pi 的 `SessionManager`（`packages/coding-agent/src/core/session-manager.ts`）与 `packages/durable` 的 entry 采用的正是 DemoClaw 文档描述的形态：

- **`parentId` 树**：pi 的 entry 有 `id` / `parentId`（`session-manager.ts:60`），fork 用的 `parentSession` / `branch_summary`（fork 树 + 新分支的 summary 边界），与 DemoClaw 的「entry 带 parent_id、活跃叶、fork 拷贝前缀」是同一棵树的两种表达。
- **重放 = 从活跃叶沿 parent 链回溯，遇到边界停**：pi 的 `buildContextFromEntryTree`（`session-manager.ts:412` 沿 `parentId` 走）与 `head`（最新 CompactionEntry 之前的 entry 不再进入上下文）就是 DemoClaw 的「边界 entry 之后至活跃叶」——这是 harness 五要素里「上下文管理」的落点。
- **compaction 两段式**：pi 的 `CompactionTask`（`packages/durable/src/harness/compaction.ts`）明确「事务外调模型生成摘要 → 事务内写入 CompactionEntry」，与 DemoClaw 压缩节的「事务外生摘要、事务内写 entry、校验活跃叶未变」同构；pi 甚至有 `selectCut` 的「切点必须落在完整对话上、不在工具结果中间切」（`isCandidate`），与 DemoClaw 的切点选择约束逐字一致。

### 3. 内核/扩展边界：都把「扩展清单」当作注入点

pi 的内核刻意不含的东西，与 DemoClaw 的内核排除清单几乎一一对应：

| harness 要素 | pi 的注入点 | DemoClaw 的注入点 |
|---|---|---|
| 模型接入（上下文/工具上游） | `streamFn` | `stream_fn` |
| 约束（权限/审计/边界） | `beforeToolCall`/`afterToolCall` | 同名 |
| 上下文管理（裁剪） | `transformContext` | `transform_context` |
| 新输入（纠正/转向） | steering / follow-up 回调 | steering / follow-up |
| 验证/纠正（预算与终止） | `finishTurn` | `finish_turn` |
| 纠正（取消/回退） | `AbortSignal`（贯穿 streamFn 与工具） | `cancel()` API |
| 工具接口 | 工具注册表（`name`/`schema`/`execute`） | 工具注册表 + `replay` 声明 |

DemoClaw 的「工具 replay 声明」（中断后该工具能否安全重放）对应 pi 的……其实 pi 用的是更粗的「恢复时已开始的调用绝不重新执行、物化为错误」策略，DemoClaw 把可重放性提炼成了工具的一个字段——这是 DemoClaw 在 pi 之上的一个**细化**，不是照抄。

## 差异的部分（根因：生产工程 vs 教学本体）

### pi 为什么那么重

pi 的 `packages/durable` 是一套**生产工程抽象**，但不是「多进程共享存储」——`indexeddb.ts:377` 明确注释「One owner must serialize access to each database」，pi 同样是单进程。它的重量来自**可交付、可定制、可跨环境运行**这三种生产诉求：

1. **三后端 Storage 抽象**：`storage/memory.ts`、`storage/indexeddb.ts`（浏览器）、`storage/sqlite/`（Node/Cloudflare）。同一个 `Session.commit` 要在 IndexedDB 与 SQLite 之间无差别工作——这是「产品要在浏览器、本地、边缘三处跑」的代价，DemoClaw 只要 SQLite 本地盘。
2. **通用 Task 状态机**：`TaskDefinition`（`types.ts:334`）是 `initial → phases → abort` 的通用状态机，`pending/running/waiting/sleeping/completing/terminal` 六态 + `abortRequested` 幂等标记 + `restarts` 重启计数。服务于「无服务器环境里进程被无预警回收、任务必须从提交点精确恢复」——DemoClaw 单进程 asyncio，进程由用户控制，只需 run 级四 phase。
3. **幂等提交与去重**：`Storage.mintId`、`SubmissionRecord` 的 `requestId` 幂等键——跨后端、跨回收的精确去重。DemoClaw 用 SQLite `BEGIN IMMEDIATE` + 全局单调 entry id 替代。
4. **可分发插件生态**：独立 `packages/chord`（热重载插件 runtime、contracts、RPC）+ extensions 的 `tools/sections/hooks/wraps/tasks` 五类 + package-manager + npm 分发。这是「make your own / install a package」的产品承诺，DemoClaw 第一阶段明确不做插件发现、不做 Extension 束、不做包分发。

**一句话**：pi 把「一个 harness 本体」再包上了一层「可交付、可定制、可跨环境」的生产工程外壳；DemoClaw 只要 harness 本体，把外壳整个剥掉。

### 判断:剥掉外壳是否安全（风险与代价）

剥掉是安全的，前提是接受三个代价，DemoClaw 文档与技术立场已逐一明确：

- **跨环境部署不再可行**：DemoClaw 锁定「单进程 asyncio + SQLite」，不接受浏览器/无服务器。代价是未来若要上边缘或函数即服务，得回 pi 那套可恢复状态机——但那是把 harness 部署成产品的工程细节，不在五要素之内，教学不设。
- **排队中的 steering 重启丢失**：DemoClaw 技术立场写「会话 FIFO 为内存态，用户输入在任务开始前早落盘；重启丢排队中的 steering」。这是单进程内存 FIFO 的直接代价，pi 用 durable submission 队列解决。DemoClaw 接受这个取舍，「下次提交即恢复」。
- **后台任务恢复语义更粗**：pi 的 Task 有幂等 abort 标记 + 重启重排队（`abandonOnRestart`/`background`）。DemoClaw 的前台「物化中断 + 显式续跑」与后台「自动重排队 + replay=safe 才重放」是同一思想，但实现手段（一个状态机懒字段 vs pi 一整套 Task 状态机）粗得多——够教学用，缺 pi 的「任务被重启卡死 park 到 retry」这类生产边界。

### harness 五要素对照

把五要素逐一放到 pi 与 DemoClaw 上，差异的性质就清楚了：

| harness 要素 | pi 的落地 | DemoClaw 的落地 | 差异性质 |
|---|---|---|---|
| 上下文管理 | entry 树 + transformContext + compaction | 同构（重放算法 + `transform_context` + 两段式压缩） | 同，无差异 |
| 工具接口 | 工具注册表（可经 chord/packages 分发） | 工具注册表（组合根显式注册 + `replay` 字段） | 同核心，差异在「分不分发」 |
| 约束 | before/afterToolCall + 权限 + 执行边界工具 | 同名注入点 + 工作区边界 | 同机制，约束广度由教学控制 |
| 验证 | finishTurn + 证据检查 | 同 | 同 |
| 纠正 | AbortSignal 取消 + 重试/重排队 + fork/reset 回退 | cancel() + 显式续跑 + fork/reset | 同思想，恢复语义更粗 |

结论：**五要素本身 pi 和 DemoClaw 一个都不少；少掉的只是 pi 裹在五要素之外的生产工程层。**

## pi 里 DemoClaw 没抄、但值得注意的三处

三处都不是 DemoClaw 的漏洞，而是它按「极简但不减配」刻意没抄，或抄了但值得在实现时留意的——它们恰恰落在 harness 五要素之内，属于「不减配」需要守住的东西：

1. **`length` stop 整批工具调用判失败**（`agent-loop.ts:276`）。模型输出被 token 上限切断时，流式工具调用参数可能「能通过校验但已不完整」。pi 直接把这些调用全部标记为失败让模型重发，而非执行半截参数。DemoClaw 文档的 stop_reason 六态里有 `max_iterations` 但没有 `length`——实现 loop 时值得把这一个防御补进工具三段式的 prepare 层（约束要素的完整性）。
2. **工具并行 vs 顺序本身是执行策略的一部分**（`executionMode: "sequential"` + `toolExecution`）。pi 把并行/顺序工具执行做成每天线声明 + 全局限定，而不是模型自由决定。DemoClaw 第一阶段 ReAct 只走一步，但「工具批次怎么执行」是 loop 里绕不开的一个小设计点，pi 的 sequential/parallel 声明值得作为实现参照（工具接口要素）。
3. **steering 取用时机**（`pendingMessages` 只在 turn 边界取、避免一次吞两条）。这是 pi 循环里一个不起眼但正确的细节，DemoClaw 实现「steering 在工具调用间插入」时若用朴素轮询会踩到「一个 turn 内多取一条」的坑，pi 的做法可直接照搬（纠正要素）。

## 给判断的最终结论

- DemoClaw 的架构设计**整体成立**：骨架（ReAct 循环 + entry 树 + 注入点清单 + 会话树六动词 + compaction 两段式 + 恢复物化中断）与 pi 逐点同构，说明它抓准了 harness 五要素对应的必然问题，没有发明错概念。
- 与 pi 的差异**都在根因「生产工程 vs 教学本体」的投影上**：pi 是 harness 本体 + 生产工程全量，DemoClaw 是 harness 本体不减 + 生产工程全减。DemoClaw 在每一处都明确写明了取舍与代价，没有暗中减掉 harness 本体——这正是「极简但不减配」在架构层的正确表现。
- 唯一的实质风险不在架构，而在**实现**：pi 那 959 行循环里的防御细节（length 截断、parallel/sequential、steering 取用时机），是 DemoClaw 更薄的循环实现里需要注意别丢的「不减配」点。这些应写进 loop 的实现，而不是再堆一层抽象。