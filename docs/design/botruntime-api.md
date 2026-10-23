# 设计稿：BotRuntime API（第 0 步）

状态：草案 v2（已按独立检查修订），待用户评审。
依据：[architecture.md](../architecture.md)（分层、注入点、六动词、Task 六态）。
检查记录：Sonnet 独立评审发现 3 严重 + 5 中 + 5 低，三条使用测试均有缺口；本版已全部处理（见文末修订说明）。

## 目标

定义 DemoClaw SDK 的门面：外部（Web、测试、教学示例、未来的 cron）如何使用 DemoClaw。本稿只定**公开接口的形状**——函数签名、数据类型、调用约定；不做实现。

## 使用图景

```python
from democlaw import BotRuntime, BotSpec, RuntimeConfig, StopReason

# 1. 装配：一次 build，得到可用的运行时
runtime = BotRuntime.build(RuntimeConfig())    # 缺 API key 时 build 即报错并给出操作指引

# 2. （可选）选择 bot
architect = runtime.bot("architect")

# 3. 提交任务并等待结果（同步便捷形态；禁止在 async 语境调用）
result = architect.run("重构 src/utils.py 并验证测试通过")

# 4. 或异步运行 + steering + 事件观察
task = architect.start("调研三个库的优劣")     # 返回 TaskHandle，立即返回
async for event in task.events():             # 活体事件流（Web 观察面的数据源）
    ...                                        # ToolCallStarted / ToolResult / TextDelta / ...
task.steer("重点看异步支持")                    # 运行中→注入当前任务；已结束→作为该会话的下一次输入自动执行
result = task.result()                         # 同步阻塞至终态（含 cancelled）
task.cancel()                                  # 幂等；请求取消信号，result() 阻塞到真正停止

# 5. 中断恢复：重启后发现上次中断的任务
for info in runtime.sessions_with(StopReason.INTERRUPTED):
    print(f"会话 {info.id} 有中断任务，上次 phase={info.last_phase}")
    # 续跑 = 调用方显式重新提交（架构恢复语义）
    runtime.bot(info.bot_id).run("继续刚才的任务", session_id=info.id)

# 5.5 查看任务与会话全景（类似 Claude 的 show session detail）
runtime.sessions()                             # 全部会话：含各自 running_task_id / active_entry_id
architect.session_tasks(sid)                   # 一个会话的任务历史：每次 run 一行
architect.task(task_id)                        # 单任务详情：状态、事件摘要、工具调用记录

# 6. 会话树操作
session = architect.new_session()
architect.sessions()                           # 含 active_entry_id 与 running_task_id
architect.fork_session(sid, at_entry_id)       # at_entry_id 必须是该会话树上的节点，否则抛 InvalidEntry
architect.reset_session(sid, to_entry_id)      # to_entry_id 必须是祖先节点；忙碌时抛 SessionBusy
architect.delete_session(sid)                  # 仅全空闲；有 fork 子树时级联删除并确认

# 7. 读历史（dump 的 API 化）
entries = architect.history(sid)               # entry 树的线性投影（带 parent 关系）

# 8. 用完即走
runtime.close()
```

## 调用形态与事件循环契约

**门面只有两个形态，规则简单**：

- **同步语境**（教学示例、脚本）：用 `run()`（阻塞至终态返回 TaskResult）、`start()` + `task.result()`（阻塞等待）。内部由 BotRuntime 拥有一个事件循环线程，同步调用经它调度。
- **async 语境**（Web 后端、asyncio 应用）：用 `await bot.arun()`、`start()` + `await task.aresult()`、`async for event in task.events()`。若检测到调用线程已有运行中的事件循环，同步 `run()` 抛 `RuntimeError` 并提示改用 async 形态——**同一语境只用一种形态**，不做"既阻塞又可 await"的双语境方法（语言层面不可行）。

## 公开类型

```python
class StopReason(Enum):
    COMPLETED = "completed"
    ERROR = "error"
    MAX_ITERATIONS = "max_iterations"
    CANCELLED = "cancelled"
    EMPTY_RESPONSE = "empty_response"
    INTERRUPTED = "interrupted"     # 只由恢复流程设置

class TaskPhase(Enum):
    QUEUED = "queued"               # 已提交，worker 未取（steer 在此态抛 NotRunning）
    AWAITING_TOOLS = "awaiting_tools"
    TOOLS_COMPLETED = "tools_completed"
    FINAL_RESPONSE = "final_response"   # 终态后 stop_reason 生效

class EntryKind(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    COMPACTION = "compaction"
    RESET = "reset"
    INTERRUPTED = "interrupted"     # 恢复时物化的中断结果

@dataclass(frozen=True)
class TaskResult:
    stop_reason: StopReason
    content: str                    # 最终回答（completed 时）
    error: str | None               # error/empty/max_iterations 的诊断信息（含定位上下文）
    task_id: str
    session_id: str
    iterations: int                 # 实际循环轮数（max_iterations 的触发源即此值达上限）
    tools_used: int

@dataclass(frozen=True)
class TaskStatus:
    task_id: str
    phase: TaskPhase
    stop_reason: StopReason | None  # 终态时非空
    iterations: int
    tools_used: int

class TaskHandle:
    def status(self) -> TaskStatus: ...
    def result(self) -> TaskResult: ...            # 同步阻塞至终态（含 cancelled）
    async def aresult(self) -> TaskResult: ...     # async 语境等待
    def steer(self, message: str) -> None: ...
        """追加指令，不判断调用方视角的任务状态：
        运行中 → 注入当前任务（下一轮工具调用间生效）；
        已结束/排队尾 → 追加为该会话的下一次输入（follow-up），自动开始下一次运行。
        即"继续讨论"永远可行，无需先查 status。"""
    def cancel(self) -> None: ...                  # 幂等；已终态为 no-op；result 阻塞到真正停止
    def events(self) -> AsyncIterator[Event]: ...  # 活体事件流（形状见下）

@dataclass(frozen=True)
class SessionInfo:
    id: str
    title: str | None
    bot_id: str
    active_entry_id: str            # 当前活跃叶（合法 reset/fork 参数的参照）
    running_task_id: str | None     # 忙碌标记（Web 渲染"正在跑"用）
    created_at: float               # epoch 秒，仅排序用（权威顺序靠 entry id 单调）

@dataclass(frozen=True)
class Entry:
    id: str
    parent_id: str | None
    kind: EntryKind
    content: str | dict | None      # 按 kind 解释（user/assistant: str；tool_call/tool_result: dict）
```

**事件类型**（第 5 步 events.py 实现内核侧，此处定门面形状）：

```python
@dataclass(frozen=True)
class Event:
    task_id: str
    type: str                       # text_delta / tool_call_started / tool_result / phase_changed / task_finished
    data: dict                      # 按 type 解释；text_delta 含增量文本与工具名等中间态
```

## BotRuntime / Bot 接口

```python
class BotRuntime:
    @classmethod
    def build(cls, config: RuntimeConfig | None = None) -> BotRuntime:
        """装配一切。轻量校验：缺 API key、bot spec 缺失等在此报错并给出操作指引。"""

    def bot(self, name: str) -> Bot: ...
    def bots(self) -> list[str]: ...
    def sessions_with(self, stop_reason: StopReason) -> list[SessionInfo]: ...
        """枚举含指定终态任务的会话——重启后发现 interrupted 任务的入口。"""
    def close(self) -> None: ...

class Bot:
    # 任务（两形态）
    def run(self, task: str, *, session_id: str | None = None,
            model: str | None = None, max_iterations: int | None = None) -> TaskResult: ...
        """同步阻塞；async 语境禁用（抛 RuntimeError 指引 arun）。session_id 缺省自动新建。"""
    async def arun(self, task: str, *, ...) -> TaskResult: ...
    def start(self, task: str, *, session_id: str | None = None, ...) -> TaskHandle: ...
        """提交进会话 FIFO 立即返回；phase 从 QUEUED 开始。"""
    # 任务与会话全景查询（观察面：类似 Claude 的 show session detail）
    def session_tasks(self, session_id: str) -> list[TaskStatus]: ...
        """该会话的任务历史（每次 run 一行，含终态与统计），最新在前。"""
    def task(self, task_id: str) -> TaskStatus: ...
        """单任务详情；配合 history() 即可重建任务完整过程。"""
    # 会话树
    def new_session(self) -> SessionInfo: ...
    def sessions(self) -> list[SessionInfo]: ...
    def fork_session(self, session_id: str, at_entry_id: str) -> SessionInfo: ...
        """at_entry_id 必须是该会话树上的节点（跨会话/不存在抛 InvalidEntry）。"""
    def reset_session(self, session_id: str, to_entry_id: str) -> None: ...
        """to_entry_id 必须是祖先节点；会话忙碌（running_task_id 非空）抛 SessionBusy（含 task_id）。"""
    def delete_session(self, session_id: str) -> None: ...
        """仅全空闲（require_idle 语义），有排队任务也拒绝；级联删 fork 子树。"""
    def history(self, session_id: str) -> list[Entry]: ...
```

## 异常与错误约定

| 层 | 方式 | 例 |
|---|---|---|
| 装配/调用错误（用法不对） | **抛异常**，消息含操作指引 | `build` 缺 API key / bot spec 不存在 / `run` 在 async 语境 / `reset` 忙碌（`SessionBusy`，含 task_id）/ `fork/reset` 参数非树内节点（`InvalidEntry`）/ `delete` 非全空闲（`SessionBusy`） |
| 任务执行错误（运行期） | **编码进结果**，不抛 | `TaskResult.stop_reason ∈ {error, empty_response, max_iterations}` + `error` 字段带定位上下文——与内核 stream_fn 契约一致 |

## 配置

```python
@dataclass
class RuntimeConfig:
    db_path: str | Path = Path.home() / ".democlaw" / "democlaw.db"
    bots_dir: str | Path = Path("bots")            # 相对 cwd 解析为绝对路径
    workspace_root: str | Path = Path("workspace") # 同上
    model: str = "openai/gpt-4o"                   # 全局默认（三层：任务>会话>全局）
    api_key_env: str = "OPENAI_API_KEY"            # 只存环境变量名，不存值
    max_iterations: int = 50
    # 注入口（测试与高级用法；生产用默认装配）
    stream_fn: Callable | None = None              # 装配期注入 mock 模型（AGENTS.md 测试确定性）
    tools: dict[str, Tool] | None = None           # 装配期注入自定义工具集
```

会话层模型（Web 切换用，存储于会话属性）与任务层模型（`run(model=...)`）在各自入口覆盖。

## 关键设计决策

1. **同步便捷 + async 完整，不搞双语境方法**：`run/arun`、`result/aresult` 分开；同步形态禁止在 async 语境使用（抛错并指引）。事件循环由 BotRuntime 拥有，线程归属在文档写明。命名遵循生态惯例：协程是 `arun`，`start` 提交拿句柄。
2. **run 不传 session 自动新建**——教学最短路径一行可用；会话延续显式传 `session_id`。
3. **Bot 是无状态视图**：不持运行态，每次调用落 storage；同名多次取等价。
4. **steer 不要求调用方判断状态（用户修订）**：运行中 → 注入当前任务；已结束/排队尾 → 追加为该会话的下一次输入自动执行。"继续讨论"永远可行。这覆盖了 steering（运行中转向）与 follow-up（结束后续聊）两个内核概念，门面统一为一个入口。
5. **事件流在门面定形状、第 5 步实现内核**：`task.events()` 是 Web 观察面的唯一活体通道（text_delta 含增量文本与工具中间态）；历史/重连永远查存储（架构不变量 6）。
6. **`sessions_with(StopReason.INTERRUPTED)` 是恢复的发现入口**：重启后枚举中断会话，续跑 = 调用方显式重新提交（架构恢复语义）。
7. **delete 补齐**：六动词中 append/compact 是内部动词（经 FIFO/压缩扩展），new/reset/fork/delete 是门面动词——闭表暴露完整。
8. **密钥只经环境变量**，build 时校验并给可操作报错。
9. **不做的事**：工具运行时注册（装配期组合根决定，`RuntimeConfig.tools` 是唯一注入口）、compaction 手动触发（自动阈值足够）、跨进程 API。

## 修订说明（对照检查报告）

- A1：delete 补齐 + 决策 7 说明内部/门面动词划分
- A2：新增「异常与错误约定」一节，SessionBusy/NotRunning/InvalidEntry 定义
- A3：reset/fork 参数契约写明（祖先约束、InvalidEntry）；SessionInfo 补 active_entry_id
- A4/B5a：事件流 `task.events()` 进门面（决策 5）；SessionInfo 补 running_task_id
- A5/C2：TaskPhase/EntryKind 做 Enum；Entry.content 收窄为 str|dict|None
- B1/C1：废除双语境 result，run/arun、result/aresult 分开 + 事件循环契约一节 + 命名改 start
- B2：TaskPhase 补 QUEUED（后经用户评审修订：steer 取消 NotRunning，见用户修订 1）
- B3：cancel 语义写明（幂等、no-op、result 阻塞到真停）
- B4：build 轻量校验 + 路径绝对化规则
- B5b：RuntimeConfig 补 stream_fn/tools 注入口（测试确定性）
- C3：created_at 改 epoch float 并注明弱信息
- C4：content_so_far 从 TaskStatus 移除（流式经 events()，状态查询只有结构化字段）
- B6：max_iterations 注明触发源

用户评审修订（2026-10-23）：

1. **steer 语义重定义**：不再要求调用方判断任务状态（原"仅 running 时接受，否则抛 NotRunning"不合理）。新语义：运行中 → 注入当前任务；已结束/排队尾 → 追加为该会话的下一次输入（follow-up）自动执行。**"继续讨论"永远可行**，调用方无需先查 status。NotRunning 异常从约定表中移除。
2. **补任务/会话全景查询**（类似 Claude 的 show session detail）：`session_tasks(sid)`（会话的任务历史，每次 run 一行）、`task(task_id)`（单任务详情）；使用图景补 5.5 节。

## 评审要点（给你的提示）

1. `run/arun` 双形态 + 同步禁入 async 语境的契约是否可接受（还是干脆只做 async 形态？）
2. 事件类型 `Event(type, data)` 的形状够不够 Web 用（还是应该强类型化每个事件？）
3. `sessions_with(StopReason)` 的恢复发现入口形状
4. 命名直白度：start/arun/steer/fork_session
