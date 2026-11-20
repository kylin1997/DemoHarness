# 设计稿：多 Agent 与后台任务（第 7、8 步）

状态：草案 v2（已按独立评审修订：内核改动诚实降级、后台重排归属、restart 计数独立化、三去向测试语义修正）。
依据：[architecture.md](../architecture.md)「任务分派与 subagent」「执行策略（全部是扩展）」；[specs/agent-feature-scope.md](../specs/agent-feature-scope.md)「多 Agent」「后台任务/异步执行」；[task.md](./task.md) 恢复扫描（本稿复用其报告、不改其内核）；[events-bots.md](./events-bots.md) 事件流（Agent View 数据源）；[storage-session.md](./storage-session.md) schema（本稿新增的会话列在此登记，见决策 2）。

## 目标

实现 bot 间任务分派与后台 subagent：分派三去向（自己执行 / 派给另一 bot / 启动 subagent）、subagent 的临时会话与结果回传、后台执行与重启存活、Agent View 观察。**作为扩展/策略机制实现**——分派是策略扩展提供的工具，不是内核动词；内核只新增**一条最小会话元数据**（见决策 2），不改状态机、不加新 EntryKind、不加新 stop_reason。

## 定位重申：这是扩展，不是内核

多 Agent 用的是内核早已具备的两样东西——**新会话 + Task**（第 1、2 步）。分派（怎么拆、派给谁）是**策略扩展提供的工具**，默认 ReAct 策略不含分派。本模块的实现是：

1. 一组**工具**（delegate_subagent / assign_bot），让模型在执行任务时能发起分派；
2. 一段**编排扩展**，把「子任务的发起 → 子会话 Task 的等待/不等待 → 结果回传」串成一个策略层可复用的机制。

**内核改动的边界（本稿唯一权威）**：内核仅新增一条会话级元数据列（溯源 + background 标志，合并在 decision 2 说清），并明确其与既有 schema 的正交语义；**核心状态机（Task 六态、六动词、会话 FIFO、重放算法）零改动**。除此之外的一切（分派工具、后台编排、后台重排、restart 计数）都在扩展层。

## 分派三去向

| 去向 | 机制 | 生命周期 | 结果如何回父会话 |
|---|---|---|---|
| 自己执行 | 当前 bot 的循环继续（零派生） | — | — |
| 派给另一个 bot | 用**对方 bot 的 spec** 开新会话执行 | 持久（对方的会话与记忆沉淀在对方世界） | bot 的 `run()` 返回的 TaskResult，作为工具结果回填发起的工具调用 |
| 启动 subagent | 用**内联参数**（聚焦 prompt + 受限工具集 + 预算切片）开新会话执行 | 临时（身份即弃，干完销毁；执行记录照常入库，可重放观察） | `final_response` 作为工具结果返回 |

**两种派生去向**（派 bot / 启 subagent）共用同一机制（新会话 + Task），只是会话身份来源不同（具名 bot spec vs 工具参数内联）——**一机制两来源**；「自己执行」是零派生的基线去向，不产生新会话。**子会话与父会话之间是一条溯源边（谁派生的我），不是 fork 边**——语义见决策 2；子会话**不进父会话的上下文**（不发生上下文污染）。

## 分派工具形状（策略扩展提供）

```python
@dataclass(frozen=True)
class SubagentInput:
    prompt: str                # 聚焦 prompt：只描述这一个子任务
    tools: list[str] | None    # 受限工具集（None=继承默认）；white-list 语义，subagent 只见这些
    budget: int | None         # 预算切片（max_iterations 覆盖；None=继承全局）
    bot: str | None            # 非空 = 派给另一个 bot（用对方 spec 开会话）；空 = 内联 subagent
    wait: bool = True          # True=前台等待结果；False=后台不等待（第 8 步）

# assign_bot(name, prompt, *, wait=True) -> ToolResult   —— bot 非空、tools/budget 从对方 spec 继承
# delegate_subagent(prompt, tools, budget, *, wait=False) -> ToolResult —— 内联参数、可后台
```

两个工具是同一机制的两个入口，参数经 ToolCall.params 传（第 3 步落盘映射原样可序列化）。工具返回的 `ToolResult.content` = 子会话的 `final_response` 文本；`details` 承载子会话 id、子 Task 结果、溯源链接。

## 后台执行（第 8 步）

`wait=False` 即后台：发起后不等待，父会话继续当前 turn。后台任务是一个**独立的 background session**（子会话），完成后结果作为 follow-up 输入回投父会话——「回投走 follow-up、不自动摘取上下文」见架构文档「任务分派」节，本稿不重复其权威。

### 重启存活与后台重排（本稿替换 task.md 的挂账表述）

**归属**：内核 `scan_and_recover`（task.md）**只报告、不续跑**——它返回的 `RecoveryReport` 必须携带 `session_id` + `is_background` 标志 + 未终态 phase，供**扩展层（BotRuntime / 后台编排）**决定「前台：物化停 + 显式续跑」还是「后台：自动重排队」。自动 re-queue 的编排逻辑 100% 在 core 之外；内核不改分叉行为，只把 background 标志随报告带出。

**后台任务崩溃时可能停在的 phase 与重排后的行为**（一格一格列清，消除「重放边界在哪个窗口」的歧义）：

| 崩溃时的 phase | 已落盘事实 | 重排后行为 |
|---|---|---|
| awaiting_tools 之前（仅 user entry，或有 assistant 纯文本未带工具调用） | 无任何工具调用落盘 | 直接续跑（重新调用模型，无副作用可重放可言） |
| awaiting_tools（assistant 带 tool_calls 已落盘） | 工具调用已落盘、结果未落盘 | 若该批全部声明 replay=safe → 「safe 工具续跑」（在**新分支**重跑这批工具，见下）|
| tools_completed 部分 | 部分结果已落盘、后续待定 | 已落盘结果的调用**绝不再执行**（不变量 5）；未落盘的 safe 调用同上一行 |

**「safe 工具续跑」≠ 读路径重放**：重放算法是**读路径**（组装模型上下文），本表的「重跑」是**执行路径**（重新执行一批声明了 safe 的调用）。两者必须区分命名，不得混用「重放」一词。续跑落**新分支**（fork 出一个续跑会话），原分支保留已物化的 interrupted——这样每次重启各留一条 interrupted、计数可数（见下），且与原会话历史不互相污染。

**restart 计数（独立计数器，不用 interrupted entry 数）**：`interrupted entry` 因物化幂等（恰好一次）而在同一分支**永远只有一条**，数不出「重启了几次」。故重启次数由**后台扩展持有的持久化计数器**记录——落在新增的会话元数据列（`restart_count`，见决策 2），按 background session 记，每次自动重排 +1，**超过 3 停车待人工**。fork/reset 不改变此计数（它按会话行、不按树）。

## Agent View（观察侧）

后台任务列表（Task 状态 + 子会话入口），数据源是 events-bots.md 的 EventBus + 存储快照：

- **列表的唯一判别谓词 = 会话带 background 标志**（持久化，见决策 2），不含「running_task_id 非空」——前台 running 会话与后台列表是两回事，不混入。
- 每个后台任务是一个可观察的 background session（点进去即它的 history / task）。
- 回投完成时发 `task_finished`（事件已含 task_id / session_id 归组键），Agent View 做覆盖式刷新。

**做/不做的边界**：Agent View 需要一条**会话级持久化 background 标志**（不是「投影而已」，因为它要跨重启可读——事件流不是历史源、内存态 running 进程死后即失）。不做：后台任务队列的持久化调度编排（进程内 re-queue 足够，教学规模）、跨进程后台执行、父会话上下文自动摘取子结果。

## 与其他模块的接口

| 模块 | 契约 |
|---|---|
| 内核会话/Task | 复用新会话 + Task；内核仅增会话元数据列（决策 2）；核心状态机零改动 |
| storage-session | 本稿新增的会话列（origin_parent_id / background / restart_count）在此登记，列语义与 fork parent_id 正交（决策 2） |
| tools.py | delegate_subagent / assign_bot 是标准 Tool（第 3 步协议），进注册表、受白名单约束 |
| task.py | 复用 `scan_and_recover`，只读它的报告、不改它的内核逻辑；自动重排全在扩展层 |
| events.py | task_finished / task_started 供 Agent View 刷新 |
| 门面 API | 分派由模型经工具发起；Agent View 查询 = `sessions()` 过滤 background + 既有 task()/history() 组合，可能加 `background_tasks()` |

## 关键设计决策

1. **一机制两来源**：两种派生去向（bot spec vs 内联参数）共用「新会话 + Task」，不新增内核运行时概念。
2. **内核改动 = 一条最小会话元数据增量**（本稿把评审按下的「零改动」诚实地降级）：sessions 表新增
   - `origin_parent_id`（溯源：谁派生的我）——与既有 `parent_id`（fork 树边）**正交**：fork 边沿 delete 协议级联删、参与重放回溯；溯源边仅指 parent→child 的派生关系，不参与重放、不进 delete 级联、双向不同义（一个会话至多一个 origin，但可被 fork 出多个分支）。subagent 会话的 `origin_parent_id` 指向发起它的父会话（跨 bot 亦可）。
   - `background`（布尔）——唯一判别后台任务的标志；跨重启可读；fork 不继承（子会话是临时的、不可被 fork 出「后台副本」）；reset 保留。
   - `restart_count`（整数）——后台重启计数（决策 4）。
   三条列一次说清，是「内核零改动」口号下的唯一真实让步；**Task 六态、六动词、FIFO、重放算法、EntryKind、stop_reason 全部零改动**。
3. **子会话不进父上下文**：结果仅作为 follow-up 输入回投（架构文档既有约束），不自动摘取到父上下文——防污染（新边界是本条「不进上下文」，非「走 follow-up」本身）。
4. **后台重排 = 内核报告、扩展编排**：`scan_and_recover` 把 `is_background` + phase 带出报告，扩展层决定物化停或重排队；restart 计数用独立持久化计数器（非 interrupted entry 数），超 3 停车。
5. **safe 工具续跑 ≠ 读路径重放**：续跑是执行路径、落新分支；与不变量 5（已落盘未完成的调用绝不重执行）通过「只重跑未落盘结果的 safe 调用、已落盘结果的分支不再动」对齐。
6. **Agent View = 存储 + 事件的投影**，但「background 标志」是必须的持久化事实（不是零持久化），已归入决策 2 的会话列。

## 测试清单（验收标准）

1. assign_bot：用镜像 spec 的对方 bot 开会话执行，结果作为工具结果回填；子会话带 origin 溯源、不进父上下文
2. delegate_subagent：内联参数生效（受限工具集只暴露声明的、预算切片生效），final_response 回填
3. **三去向同源（不通测等价）**：同一子任务分别走「派 bot」「启 subagent」，断言二者都经「新会话 + Task」机制、各自落到对应会话身份来源（对方 bot world vs 内联即弃）、溯源边正确——**不断言结果一致**（两者工具集/系统提示天然不同，结果必然可能不同）
4. 前台 wait=True：父 turn 阻塞至子 Task 终态；子失败 → 工具结果 is_error=True
5. 后台 wait=False：父 turn 立即继续；子完成后 follow-up 回投父会话 FIFO
6. 重启存活：后台 Task 未终态 → 恢复扫描报告带 is_background → 扩展层自动重排；awaiting_tools 前（无工具落盘）→ 直接续跑
7. safe 续跑边界：awaiting_tools 且全 safe → 新分支重跑这批工具；副作用工具 / 已落盘结果的调用 → 物化中断不重执行
8. restart 计数：同一 background session 连续崩溃重排，restart_count 递增；>3 → 停车待人工（不无限循环）；fork/reset 不改变计数
9. Agent View：background 会话列表正确；回投时 task_finished 触发刷新
10. **内核改动边界断言**：新增会话列仅 origin_parent_id / background / restart_count 三条；Task 六态、六动词、EntryKind、stop_reason 全集、重放算法均无改动（跑上述用例前后 core/ 的状态机相关 diff 为空）

## 留用户决策（已定）

**分歧 8：内核梯度——已定「增三条会话列」**（用户拍板默认：内核增 origin_parent_id / background / restart_count 三条列，其余零改动；不采用扩展层 sidecar 备选）。三列已在 storage-session.md schema 与决策 9（fork/reset 拷贝策略）登记。

## 评审要点（给用户）

1. 分歧 8 的「增三条列 vs 完全零改动」
2. 后台重排「内核报告、扩展编排」的归属划分
3. restart 计数独立化 + safe 续跑落新分支
4. 补一个真实模型场景（多 bot 协作 + 后台任务 + kill 重启）作为本模块的端到端证据（正文交给 scenarios.md）
