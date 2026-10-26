# 设计稿：storage + session 模块（第 1 步）

状态：草案 v2（已按独立检查修订），待用户评审。
依据：[architecture.md](../architecture.md)「数据形态」「不变量清单」「附录：实现期约束」；API 形状见 [botruntime-api.md](./botruntime-api.md)。
检查记录：Sonnet 独立评审发现 2 严重（fork 快照、delete 竞态）+ 2 高（reset entry 语义、running 状态机）+ 若干中低；本版全部处理（见文末修订说明）。

## 目标

实现数据地基：entry 树存储（SQLite）、六动词与守卫、重放算法、会话 FIFO 串行化。这是所有能力域的承载层——循环、恢复、压缩、会话树全部建在它上面。本模块完成后，第 0 步的 API 中会话树动词与 history 即可实现。

**不变量责任划分**（architecture.md 七条）：本模块负责 1（FIFO 串行）、2（entry 不可变）、3（commit 原子）、4（id 单调）、6（历史只从 entries 重建——本模块是 entries 的唯一权威，事件流第 5 步）；不变量 5（恢复物化）与 7（六态权威）归第 2 步 task.py。

## 文件与职责

```text
core/storage.py   # SQLite 访问层：schema、读写原语、事务。唯一碰 SQL 的地方。
core/session.py   # 会话拥有者：FIFO、动词编排（调 storage）、守卫、重放算法、running 登记。
```

分层原则：storage.py 不懂业务语义（不知道什么是 reset），session.py 不写 SQL。

## Schema

```sql
CREATE TABLE sessions (
    id            TEXT PRIMARY KEY,           -- "s_" + 单调序号（如 s_7）
    bot_id        TEXT NOT NULL,              -- 命名空间隔离键
    parent_id     TEXT,                       -- fork 来源会话（根会话为 NULL）
    fork_at       TEXT,                       -- fork 分叉点 entry id（同上）
    active_leaf   TEXT,                       -- 当前活跃叶 entry id（空会话为 NULL）
    model         TEXT,                       -- 会话层模型覆盖（三层解析中间层；NULL=用 bot spec/全局）
    title         TEXT,
    created_at    REAL NOT NULL,
    -- 多 Agent / 后台任务三列（第 7、8 步扩展用，语义与 fork 正交——
    -- 详见 multi-agent.md 决策 2）：
    origin_parent_id TEXT,                    -- 溯源：谁派生的我（subagent/派 bot 的父会话）。
                                              --   与 parent_id（fork 树边）正交：不参与重放、不进 delete 级联
    background     INTEGER NOT NULL DEFAULT 0,-- 后台任务标志（0/1）：re-queue 与 Agent View 的判别谓词；
                                              --   fork 不继承、reset 保留
    restart_count  INTEGER NOT NULL DEFAULT 0 -- 后台重启计数：每次自动重排 +1，>3 停车待人工；
                                              --   按会话行记，不随 fork/reset 变
);

CREATE TABLE entries (
    id            TEXT PRIMARY KEY,           -- "e_" + 单调序号，全局单调（不变量 4）
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    parent_id     TEXT REFERENCES entries(id),-- 树链（根 entry 为 NULL）
    kind          TEXT NOT NULL,              -- user/assistant/tool_call/tool_result/compaction/reset/interrupted
    content       TEXT NOT NULL,              -- JSON 序列化（str 或 dict 按 kind）
    created_at    REAL NOT NULL
);
CREATE INDEX idx_entries_session ON entries(session_id, id);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- 存 next_session_seq / next_entry_seq：id 发号器（发号与插入同一事务，回滚一致）
```

设计要点：

- **id 应用层发号**（meta 表，事务内）——「全局 entry id 单调」由本模块自持；发号与插入同事务，中途 kill 两者一起回滚，重启后重发同号无害（该号从未被占用）。
- **active_leaf 是会话行字段**；reset/fork/compact 都只改它或加 entry。
- **PRAGMA**：journal_mode=WAL、synchronous=NORMAL、foreign_keys=ON。
- **单连接是正确性前提**（不是性能偏好）：进程内对同一 db 文件只允许一个 Storage 实例存活——`Storage.__init__` 对已打开路径登记（进程级 registry），重复打开抛错。两个连接 = 两个写事务交错 = 发号重号 + FIFO 失效。测试用临时文件各开各的库。

## Storage 层接口（core/storage.py）

```python
class Storage:
    def __init__(self, db_path: str | Path): ...   # 打开/建库；同文件重复打开抛 StorageAlreadyOpen
    def close(self) -> None: ...

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]: ...
        """唯一写通道：BEGIN IMMEDIATE → yield conn → commit/rollback。
        本层不提供任何 UPDATE/DELETE entry 的通道——树只增不改（不变量 2）。"""

    # 读原语（WAL 下走快照，无事务）
    def session(self, sid) -> SessionRow | None: ...
    def entries(self, sid) -> list[EntryRow]: ...
        """该会话全部 entry，按 id 序。重放/拷贝在 session 层内存建链，纯内存函数不额外发 SQL。"""
    def entry(self, eid) -> EntryRow | None: ...
    def sessions_of_bot(self, bot_id) -> list[SessionRow]: ...
    def sessions_all(self) -> list[SessionRow]: ...

    # 写原语（必须在 transaction() 内调用；storage 持有 SQL，session 层给语义）
    def insert_entry(self, conn, row: EntryRow) -> None: ...
    def insert_session(self, conn, row: SessionRow) -> None: ...
    def update_active_leaf(self, conn, sid, leaf_id) -> None: ...
    def delete_session_tree(self, conn, sids: list[str]) -> None: ...
        """一个事务内删除多个会话及其全部 entry（delete 级联用）。"""
    def mint_entry_id(self, conn) -> str: ...
    def mint_session_id(self, conn) -> str: ...
```

约 200-250 行。批量写原语（insert_entry 等）让 session.py 组装语义而不写 SQL。

## Session 层（core/session.py）

### 核心模型：单条会话 FIFO + SQLite 写锁兜底跨会话操作

**不再有全局 FIFO**（v1 的双 FIFO 已删——检查确认它是两颗并发雷的来源，且全局操作极少）：

- **一切操作都投给目标会话的 FIFO**：append/reset/compact/history 走本会话；**fork 也走源会话的 FIFO**（读源必须串行在源的写入之后——见动词表）；**delete 走"目标会话树里每个会话"的串行协调**（见下）；new 无源会话，直接事务。
- **跨会话一致性靠 SQLite 单写者 + BEGIN IMMEDIATE**：所有写事务天然互斥。fork 的「读源链 + 写新会话」整个放进**一个写事务**——读源在写锁内，源会话的 append（也要写锁）被挡住，快照语义由写锁保证，无需显式锁。

### 会话拥有者

```python
class SessionManager:
    def __init__(self, storage: Storage): ...
    async def submit(self, session_id: str, op: SessionOp) -> Any: ...
        """投操作进该会话 FIFO；worker 不存在则启动，队列空且操作完成即退出。
        worker 生命周期契约（pi 差异检查必改项）：每个 op 的异常（含事务失败、
        SessionBusy 拒绝）回给对应 submit 的 Future，worker 继续；worker 循环
        自身的未捕获异常必须被兜住并重启 worker——队列不允许因异常死亡
        （否则该会话后续 submit 永久挂起）。"""
    # running 登记（守卫的任务级信号——状态机见下节）
    def mark_running(self, sid: str, task_id: str) -> None: ...
    def clear_running(self, sid: str) -> None: ...
    def running_task(self, sid: str) -> str | None: ...
        # AppendOp 携带 task_id：mark_running 在任务的输入 append 提交后由调用方（loop）调用，
        # clear_running 在 Task 终态落盘后调用。QUEUED→running 的窗口由"输入先落盘"关闭：
        # 输入 append 一提交，running 即登记——reset 校验在 FIFO 里必晚于该 append。
```

**running 状态机**（本模块定死，第 2 步 task.py 实现登记方）：

```
登记：任务输入的 AppendOp 提交成功后，loop 调 mark_running(sid, task_id)
清除：Task 终态（stop_reason 落盘）后，loop 调 clear_running(sid)
不变式：running_task(sid) 非空 ⟺ 该会话有任务在输入已落盘、终态未落盘之间
测试钩子：mark_running/clear_running 即公开方法，测试直接调用模拟忙碌（无需 task.py）
```

`SessionInfo.running_task_id` 即 `running_task(sid)` 的值（不是队列长度——排队 steering 时队列非空但无 running task）。

### 动词实现（worker 内执行，每个一个 storage.transaction）

| 动词 | 走哪个 FIFO | 事务内容 | 守卫（事务/FIFO 内校验） |
|---|---|---|---|
| new | 无（直接事务） | mint sid + 建会话行 | 无 |
| append | 本会话 | mint eid → 校验 parent==active_leaf → insert → 更新 active_leaf | running 语义由调用方（loop）管理 |
| fork | **源会话** | **单事务**：读源链（写锁内）→ mint 新 sid+新 eid 序列重映射拷贝 → insert 新会话行+entry 行 | 源忙碌可照做（写锁下读到的是一致快照；进行中任务属于源会话） |
| reset | 本会话 | mint eid → 插 reset entry（parent=to_entry_id，content 存「重置到此」标记）→ **active_leaf=reset entry** | running_task(sid) 非空 → 抛 SessionBusy（FIFO 串行保证校验时无并发 append） |
| compact | 本会话 | 校验 active_leaf==生成段记下的叶 → 插 compaction entry → 更新 active_leaf | 漂移即放弃（返回 False，扩展层下轮再试）——**这是最后防线**；主机制是扩展层在任务边界触发（无并发 append 时摘要才有效），协议见决策 6 |
| delete | 见下 | 删除协调协议 | 全空闲校验 |

**delete 的删除协调协议**（v2 重设计——级联涉及多个会话、各有 FIFO）：

```
DeleteOp(sid) 的执行：
1. 收集会话树（sid + 全部 fork 后代）
2. 对树中每个会话：
   a. 若其 FIFO 非空或 running_task 非空 → 抛 SessionBusy（全空闲含"队列空"）
   b. 停其 worker：tombstone 标记，后续 submit 直接抛 SessionNotFound（防删后新写入）
3. 单事务 delete_session_tree(全部 sids)
4. 清理内存态（FIFO 注册表、running 登记项）
```

tombstone 是内存态（session.py 的 dict），与 storage 层的行删除在同一协调流程内完成；顺序保证：校验→tombstone→删行，期间该会话 FIFO 已空且不再接受新 submit，无"删完又冒出写入"窗口。

### 重放算法（纯内存函数）

```python
def replay_entries(all_entries: list[EntryRow], leaf_id: str | None) -> list[EntryRow]:
    """all_entries = storage.entries(sid)（已按 id 序）；先建 id→entry 索引，
    从 leaf 沿 parent 向老走，遇 compaction/reset 边界停；
    返回按时间序 [边界 entry, ..., leaf]。"""
```

**reset entry 的上下文语义**（v2 明确——检查发现 v1 偷换了"重置点"）：

- reset entry 的 content 是**空标记**（不含内容），它的 parent=to_entry_id，此后用户/任务的新 entry 都接在 reset entry 之后
- **模型上下文 = reset entry + 其后全部**——即重置后模型看到的是「（重置标记）+ 重置点之后发生的一切」，**to_entry_id 本身及其之前的内容不再出现**。这与架构「模型只见重置点之后的条目」一致：reset 到 to_entry_id 表示"从此处重新开始"，而非"回看 to_entry_id"
- **与 compaction 的对称性**：compaction entry content=摘要（过去的内容压缩进来），reset entry content=空标记（过去的内容直接丢弃）——同一重放公式，两种边界语义
- **历史完整性**：reset entry 在树里可见，`history()` 与 audit 不受影响

## 与门面 API 的对接

| 门面调用 | 落到 |
|---|---|
| `new_session` | NewOp（直接事务） |
| `fork_session(sid, at)` | submit(源sid, ForkOp)（InvalidEntry=at 不在源树内） |
| `reset_session(sid, to)` | submit(sid, ResetOp)（InvalidEntry=非祖先；SessionBusy=running） |
| `delete_session(sid)` | DeleteOp 协调协议（SessionBusy=树中任一会话非全空闲） |
| `history(sid)` | submit(sid, HistoryOp) |
| `sessions()` / `sessions_with()` | storage 直读（元数据，不走 FIFO） |
| SessionInfo.active_entry_id | sessions.active_leaf |
| SessionInfo.running_task_id | running_task(sid) |

## 关键设计决策

1. **单 FIFO + 写锁兜底**（v2 重设计）：删除"全局 FIFO"抽象。所有操作走目标会话 FIFO；跨会话一致性由 BEGIN IMMEDIATE 写锁天然串行（fork 单事务读+写）。少一个抽象、消两颗雷。
2. **fork 读源在写事务内**：快照一致性靠 SQLite 写锁（源 append 同样要写锁，自动排队），不靠"无锁快照"口号。
3. **id 应用层发号**，发号与插入同事务（回滚一致、跨重启单调、e_42 可读）。
4. **reset 写 reset entry、活跃叶=reset entry**：重放公式与 compaction 统一（边界即停止点）；语义 = "从 to_entry_id 重新开始"（to_entry_id 及其之前不进后续上下文）。**这是对架构「活跃叶移到历史节点，模型只见其后内容」的实现解释：'其后'= reset entry 之后。**（评审确认点：接受这个解释吗？）
5. **running 状态机本模块定死**：登记=任务输入 append 提交后、清除=Task 终态落盘后；mark/clear 是公开方法兼测试钩子；reset/delete 守卫查它（FIFO 串行保证无校验竞态）。
6. **compact 双防线**：主机制=扩展层在任务边界触发（无并发 append，摘要有效）；最后防线=提交段校验活跃叶漂移（漂移放弃重试）。漂移检测**不是**主机制——若扩展层在错误时机触发，compact 会持续失败而非数据错误。
7. **storage 单实例硬约束**：同 db 文件进程内只允许一个 Storage（重复打开抛错）——两连接=写事务交错=发号/FIFO 失效。
8. **不做**：连接池、多进程锁、JSONL 后端、迁移框架（schema v1 起步）。
9. **会话附属状态承接约定**（pi 差异检查建议）：会话级附属状态（模型覆盖、多 Agent 三列等）以 sessions 列承接，fork 拷贝策略逐字段定——model 列：fork 不继承、reset 保留；origin_parent_id：fork 不继承（子会话不可 fork 出「后台副本」）、reset 保留；background 与 restart_count：fork 不继承、reset 保留（归本会话行的执行身份，不随树拷贝）；usage 统计若做落 assistant entry 元数据——**不引入 pi 的 document 抽象**，只在某类状态同时需要"asOf fork + 增量更新 + watch 三件套"时再议。

## 测试清单（验收标准）

1. schema 建库与 reopen 幂等；同文件二次打开抛 StorageAlreadyOpen
2. 六动词事务原子性：事务内 mock 抛 → 无半状态（entry 与 active_leaf 同回滚）
3. entry/session id 跨重启单调；发号回滚后重发同号无害
4. append parent 一致性防御（ConcurrentWrite）
5. fork：at 在/不在树内（InvalidEntry）；前缀正确；新 id 重映射；源忙碌（running 标记 + FIFO 有 append）时 fork 仍成功且快照一致（断言拷贝链完整性）
6. reset：祖先校验（InvalidEntry）；reset entry 语义（重放上下文=reset 后内容）；mark_running 后拒绝（SessionBusy）、clear 后通过——**直接调 mark/clear 模拟，无需 task.py**
7. compact：任务边界触发时成功（重放含摘要）；手动制造漂移（摘要期间插入 append）→ 放弃返回 False
8. 重放：无边界全量、compaction 边界、reset 边界、reset 后又 compact（链上多边界取最近）
9. FIFO 串行化：并发 append 线性化；HistoryOp 在 append 后可见
10. delete：级联 fork 子树；树中任一会话 running 或队列非空 → SessionBusy；删除后 submit 抛 SessionNotFound；删除后无孤儿 entry
11. bot_id 过滤：sessions_of_bot 只见自己的
12. WAL 恢复：append 事务中途 kill（模拟：开事务不提交即断开）→ 重开库无半状态

## 评审要点（给你的提示）

1. **决策 4**：reset 语义 = "从 to_entry_id 重新开始"（to_entry_id 及之前不进后续上下文）——这是架构「模型只见重置点之后的条目」的实现解释，确认这个理解
2. **决策 1**：单 FIFO + 写锁兜底（删除全局 FIFO）——简单性 vs 检查者认可的方向
3. **delete 协调协议**：tombstone 内存态是否足够（vs 持久化 tombstone）
4. running 状态机（登记/清除时点）作为第 2 步的契约是否清晰

## 修订说明（对照检查报告）

- B-1（严重）fork 快照：读源挪进写事务（决策 2）；ForkOp 改走源会话 FIFO
- B-2（严重）delete 竞态：新增删除协调协议（校验→tombstone→单事务删行）；全空闲定义补"队列空"；测试 10 补删后行为
- A-3（高）reset entry 语义：明确"重新开始"语义与 compaction 的对称解释（决策 4 + 重放节）
- A-4/B-5（高）running 状态机：登记/清除时点定死，mark/clear 公开为测试钩子（决策 5）
- A-1（中）compact 档位：写明"漂移检测是最后防线，主机制是任务边界"（决策 6），对架构"阻塞式"的有意降级显式化
- A-2（中）不变量责任：开头显式划分 1/2/3/4/6 归本模块、5/7 归 task.py；storage 注释声明"无 UPDATE/DELETE 通道"
- B-3（高）compact 协议：决策 6 + 测试 7 两条路径（成功/漂移放弃）
- B-4（中）单连接：升格为硬约束（决策 7）+ StorageAlreadyOpen + 测试 1
- C-1（中）原语补齐：insert_entry/update_active_leaf/delete_session_tree 批量写原语进 storage；重放声明为纯内存函数
- C-2（中）running_task_id：AppendOp 带 task_id、running_task 查询替代队列长度
- C-3（低）忙碌测试：mark/clear 即钩子，测试 6 直接调用
- D-1 教学粒度：三处卡点（fork 事务边界、running 状态机、重放数据源）均已写到可码粒度
- D-2（低）双 FIFO：**采纳删除**（决策 1）
- 实现测试 (a) 三连场景：v2 下 fork 单事务快照成立、reset 被 running 拒绝、compact 依赖任务边界——推演通过
- 实现测试 (b)：发号与插入同事务的回滚一致性已写明（决策 3）
- 实现测试 (c)：mark/clear 钩子即答案
