# 设计稿：task 模块（第 2 步）

状态：v2（已与 loop-providers.md 收敛：task.py 降为纯函数库，循环归 loop.py；对应表以 loop 稿「模块收敛」节为唯一权威）。
依据：[architecture.md](../architecture.md)「Task：一次任务运行的状态机」「恢复」；接口契约见 [storage-session.md](./storage-session.md) 决策 5（running 状态机）。

## 目标

实现 Task 状态机（run 级三相 checkpoint + 六态 stop_reason）与恢复扫描（物化中断恰好一次）。完成后：循环（第 4 步）有了执行骨架，恢复能力有了落地。

## Task 存储

复用第 1 步的 sessions/entries 两表，**不新建表**——Task 的进度就是 entry 流水本身：

```
一次 run 在树上的投影：
  user entry（任务输入，loop 提交的第一个 append）
    → assistant entry（含 tool_calls）
    → tool_result entry × N
    → assistant entry …（循环）
    → assistant entry（最终回答）
  循环的"checkpoint"即已落盘的 entry 前缀
```

**决策：Task 不落独立的 task 表（v1 设想推翻）**。理由：

1. 架构文档定义 Task = "phase 标记进度，每次推进先写存储"——写的内容就是 entry；phase 可从 entry 推导（叶子是 tool_result → 待模型；叶子是 assistant+tool_calls → 待工具）
2. 少一张表、少一套同步（entry 与 task 状态的一致性维护是第 1 步检查里最容易埋雷的跨事务问题——hermes 的 admission 账本正是为此付出三张表的代价）
3. TaskResult/TaskStatus（门面类型）由查询函数从 entries 投影生成

**待评审的风险**：无独立 task 表意味着 `sessions_with(StopReason.INTERRUPTED)` 的查询要靠扫描 entry（找 interrupted kind 的 entry）——数据量大时慢，教学规模可接受；且 TaskResult 的 stop_reason 存哪？（见「stop_reason 的落点」）

## stop_reason 的落点

六态 stop_reason 是终态权威（不变量 7）。两个候选：

- **方案 A**：stop_reason 写在最后一条 entry 的 content 元数据里（assistant entry 的 content dict 加 `stop_reason` 字段；interrupted 物化 entry 本身就是终态标记）
- **方案 B**：sessions 表加 `last_task_result` JSON 列（会话级最新终态）

倾向 **A**：entry 流水自足（不变量 6 的精神：历史事实只来自 entries），B 会引入第二事实源。门面查询 = 找会话树最近分支上最后一条 entry 读元数据。

## Task 状态机（core/task.py）

> **v2 职责重划（与 loop-providers.md 收敛，评审裁决）**：task.py 是**纯函数库**——phase 推导、stop_reason 落点、恢复扫描、物化函数。**while 循环只在 loop.py**（第 4 步），task.py 不再有 TaskRunner.run 的循环体。三相 ↔ loop 步骤 ↔ append 落点的对应表以 [loop-providers.md](./loop-providers.md)「模块收敛」节为唯一权威。

```python
# ── phase 推导（纯函数，loop 每轮调用） ──
def decide(entry_prefix: list[EntryRow]) -> NextStep: ...
    """从活跃分支前缀推导下一步：叶子是 tool_result → 该调模型；
    叶子是 assistant+tool_calls → 该执行工具；……单一事实源（entry）。"""

# ── stop_reason 落点（方案 A：终态 entry 元数据） ──
def terminal_content(text: str, stop_reason: StopReason) -> dict: ...
    """loop append 终态 assistant entry 时调用——写进 content 的元数据字段。"""

# ── 恢复物化（异常路径与重启共用，恰好一次） ──
async def materialize_interrupted(session_mgr, sid: str) -> None: ...
    """单事务：无结果的 tool_call 物化中断结果 + interrupted entry → active_leaf。
    loop 的异常 finally 调它；BotRuntime 打开存储的恢复扫描也调它——
    interrupted entry 的存在即幂等标记。"""

# ── 恢复扫描（BotRuntime 打开存储时） ──
def scan_and_recover(storage, session_mgr) -> list[RecoveryReport]: ...
```

要点：

- **phase 不是存储字段而是推导**：`decide(entry_prefix)` 从叶子 kind 推导——单一事实源，无状态机与存储同步问题
- **running 登记的挂点在 loop**（步骤 0：输入 append 后 mark_running；finally：**终态或 interrupted 物化之后才 clear**——异常路径先 materialize_interrupted 再 clear，守卫不变量不破）
- **预算收尾的失败路径**（与 loop 稿一致）：收尾请求返回工具调用 → 丢弃取文本；文本空 → max_iterations（不是 empty_response）
- stop_reason 六态的触发源：completed=模型无工具调用 / error=stream_fn 编码错误（含 LENGTH 过渡期）/ max_iterations=预算 / cancelled=信号 / empty_response=模型空返回 / interrupted=恢复物化专属

## 恢复扫描（物化恰好一次）

```python
def scan_and_recover(storage: Storage, session_mgr: SessionManager) -> list[RecoveryReport]:
    """BotRuntime 打开存储时调用（架构「恢复」节）。
    实现体：逐会话找未终态 run → 调 materialize_interrupted（与 loop 异常路径共用同一函数，
    恰好一次由 interrupted entry 的存在保证）。"""
    # 对每个会话：
    # 1. 找"未终态 run"（判定规则，v2 修正统一措辞）：
    #    活跃分支上最后一个 user entry 之后，既无终态 entry（带 stop_reason 元数据）
    #    也无 interrupted entry。——叶子是什么 kind 不重要（tool_result 叶子=模型等待中断，
    #    同样是未终态），避免"按叶子形态分类"的遗漏
    # 2. 物化——单事务：
    #    a. 若有已落盘但无结果的工具调用（tool_call entry 无 call_id 配对的 tool_result）：
    #       插入 tool_result entry，content = {"error": "Task interrupted"}（恰好一次）
    #    b. 插入 interrupted entry（kind=interrupted, content 带 phase 快照）→ active_leaf
    #    c. **终态 entry 必写规则**：一切终态路径（含 error/empty_response）都落一条
    #       assistant entry 挂 stop_reason 元数据——物化路径由 interrupted entry 承担此责
    # 3. 不自动续跑（前台语义）；返回报告供 sessions_with(INTERRUPTED) 查询
```

幂等性：物化 entry 的存在即"已处理"标记——重扫时该分支已有 interrupted kind，跳过。**恰好一次由 entry 事实保证**，不靠内存标志（进程重启后重扫安全）。

**后台任务的重启上限**（已定，后台任务进第一阶段——详见第 8 步设计）：前台"物化+显式续跑"无循环风险；后台自动重排若无计数，replay=safe 工具每次执行都崩进程会无限循环。定案：同一任务链的 interrupted entry 计数即重启计数，超过上限（3）停车待人工；重放策略为保守物化（safe 工具半途崩溃也物化中断，不优化重放——后台任务自动续跑仅针对 phase 在 awaiting_tools 之前、且工具已声明 replay=safe 的调用，见架构后台节）。

## 与第 1 步的接口（对 storage-session.md v2 的消费）

| 契约 | 本模块用法 |
|---|---|
| mark_running/clear_running | run() 首尾调用（登记时点=输入 append 后——第 1 步已定死） |
| AppendOp 携带 task_id | 所有任务内的 append 都带（供 running_task 查询与审计） |
| 会话 FIFO | 工具结果、最终回答的 append 经 submit()——与用户 steering 同一串行化 |
| reset/delete 守卫 | 被第 1 步守护，本模块无感 |

## 测试清单（验收标准）

1. phase 推导纯函数：各叶子形态 → 正确下一步
2. 三相推进落盘顺序：entry 前缀与 phase 一致
3. 预算：超限收尾尝试成功→completed / 失败→max_iterations
4. 取消：信号后停止 append，stop_reason=cancelled
5. stop_reason 元数据落点（方案 A）：终态 entry 可读出六态
6. 恢复扫描：中断于 tool_call→物化 tool_result+interrupted；中断于模型等待→仅 interrupted；已终态→不动；重扫幂等（恰好一次）
7. mark/clear 时点：run 期间 running_task 非空，reset 被拒
8. sessions_with(INTERRUPTED) 从 entry 投影正确

## 评审要点（预留给你的提示）

1. **无独立 task 表**（最大决策）：Task 状态全部从 entry 推导——简单但查询靠扫描，接受吗？
2. stop_reason 落点方案 A（entry 元数据）vs B（会话行 JSON 列）
3. 恢复"物化中断"覆盖：只物化待执行工具调用 + interrupted 标记，不做更细粒度（对照架构恢复节四步，确认无遗漏）
