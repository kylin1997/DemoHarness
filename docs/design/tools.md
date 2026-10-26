# 设计稿：tools 模块（第 3 步）

状态：草案 v2（已按独立评审修订；两处分歧留用户决策，见文末）。
依据：[architecture.md](../architecture.md)「内核与扩展的边界」工具行、「不变量清单」5；pi 参考：`packages/agent/src/types.ts`（AgentTool 三层抽象）、`agent-loop.ts` 三段式（:718/:831/:869）、`packages/durable/src/tools/`（内置工具形态）。

## 目标

实现工具调用协议：Tool 声明、注册表、三段式执行管线、首批教学工具。内核（core/tools.py）只定协议；具体工具在 tools/ 扩展目录。

## 分层（对齐 pi 的三层抽象，Python 化）

```python
# ── 声明层：给模型看的（对应 pi Tool） ──
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict          # JSON Schema（手写 dict，不引 schema 库——教学零依赖）
    replay: ReplayPolicy = ReplayPolicy.NEVER      # NEVER / SAFE（中断后可否重放，恢复用）
    execution: ExecMode = ExecMode.PARALLEL         # PARALLEL / SEQUENTIAL（并发声明）

class ReplayPolicy(Enum):
    NEVER = "never"          # 副作用工具：恢复时物化中断（不变量 5）
    SAFE = "safe"            # 只读工具：可重放（后台任务自动续跑用）

# ── 调用载体（v2 新增——恢复与并发落盘的地基） ──
@dataclass(frozen=True)
class ToolCall:
    call_id: str             # 关联键：tool_call entry 与 tool_result entry 靠它配对
                             #（并发批次结果落盘顺序≠调用顺序，不能靠相邻顺序关联）
    name: str                # 恢复时查注册表拿 replay 声明用
    params: dict             # 恢复重放 SAFE 工具时重发用

# ── 执行层：Python 侧实现（对应 pi AgentTool） ──
class Tool(Protocol):
    spec: ToolSpec
    async def execute(self, call_id: str, params: dict,
                      cancel: asyncio.Event) -> ToolResult: ...

# ── 结果层（对应 pi AgentToolResult） ──
@dataclass
class ToolResult:
    content: str             # 模型看到的结果文本。第一阶段仅文本——多模态结果不在本阶段协议内（显式声明）
    details: dict | None     # 结构化详情（退出码/时长/截断边界等），落盘进 entry（见落盘映射）
    is_error: bool = False   # 失败编码，不抛异常
```

与 pi 的刻意差异（v2 补齐登记）：`prepareArguments` 兼容修补、`structuredContent` 机器可读结果、`constrainedSampling`、`onUpdate` 流式部分结果、`usage` 字段（token 统计归 provider 层，不在工具结果层）——均第一阶段不做；`terminate` 字段见**分歧 1**。

## cancel 传播契约（v2 明确——Event 语义与 pi AbortSignal 的差异）

`asyncio.Event` 只中断 asyncio 协程，**中断不了已启动的 subprocess**。契约：

- cancel 在工具**执行前**触发 → 即时返回 cancelled 错误结果（不执行）
- 工具**执行中**收到 cancel → 不强制 kill 子进程；由工具自身的 timeout 兜底，结果照常落盘（循环在下一轮检查点处理取消）
- 这是相对 pi AbortSignal（可中断贯穿到子进程）的**能力降级**，教学取舍：跨进程信号杀子进程的复杂度（进程组、僵尸回收）第一阶段不值

## 三段式执行管线（core/tools.py，对齐 pi agent-loop.ts :718/:831/:869）

```python
@dataclass(frozen=True)
class BeforeVerdict:          # v2：钩子形状定义（原先悬空）
    block: bool
    reason: str              # block 时进 ToolResult.content 返回模型（模型可自行调整）

ToolHooks = Callable[[str, dict], BeforeVerdict | None]   # (name, params) -> None=放行
                                                           # after 钩子：Callable[[ToolResult], ToolResult]

async def execute_tool_calls(calls: list[ToolCall], registry: ToolRegistry,
                             hooks: list[ToolHooks], cancel: asyncio.Event) -> list[ToolResult]:
    # 1) prepare 串行：查注册表（未注册→即时错误结果，模型可见"工具不存在"）→ 参数校验 →
    #    before_tool_call 钩子（block → is_error 结果，reason 给模型）
    # 2) execute：并发策略见下
    # 3) finalize 串行：after_tool_call 钩子可替换 ToolResult（含 details）
```

**并发执行规则（v2 简化）**：批内有任一 SEQUENTIAL 工具 → **整批串行**（按模型给出的 call 原序）；全 PARALLEL 才 gather。这是 pi 的保守策略——评审确认分桶方案（SEQUENTIAL 链与 PARALLEL 桶的跨桶关系）有正确性坑，教学阶段性能无差别，从简。execution 声明保留（文档性元数据 + 未来优化位）。

**参数校验边界（v2 显式化债务）**：手写校验器只支持标量参数（string/number/boolean/integer）+ 顶层 required/enum/min/max；**遇到 array/object 等复合 schema 一律拒绝该工具调用并返回可读错误**（不静默放行）。触发条件：首个需要复合参数的工具出现时，引 jsonschema 库替换手写校验（~30 行删换）。

**duration 记录**：monotonic 时长进 details（观察面显示）。

## 落盘映射（v2 新增——原先悬空的第 4 步契约）

工具调用与结果如何进 entry（**映射在 loop.py 做**——循环是唯一知道"结果该 append 到哪条链"的地方）：

```python
# tool_call entry 的 content：
{"call_id": ..., "name": ..., "params": ...}          # 即 ToolCall 的 JSON 序列化

# tool_result entry 的 content：
{"call_id": ...,                                      # 与 tool_call 配对（恢复判定"无对应结果"靠它）
 "content": <模型看到的文本>,
 "is_error": bool,
 "details": {...} | None}                             # 落盘（见下）
```

**details 落盘决策（v2，解决与不变量 6 的冲突）**：details **落盘进 tool_result entry**——历史重建永远查存储，观察面重连后必须能显示"上个工具跑了 2.3s、退出码 3"；事件流只做活体推送副本。details 不进模型上下文（组请求时 loop 只取 content）——"不污染上下文"与"可观察"两者兼得。

## 注册表（core/tools.py）

```python
class ToolRegistry:
    def register(self, tool: Tool) -> None: ...        # 装配期调用；重名抛错
    def get(self, name: str) -> Tool | None: ...
    def definitions(self) -> list[ToolSpec]: ...
        """给 stream_fn 的工具声明清单。按 name 稳定排序（prompt cache 友好）。"""
```

注册在 BotRuntime.build 装配期完成；RuntimeConfig.tools 注入口直接喂给它。

## 首批教学工具（tools/ 扩展目录）

```text
tools/read_file.py     # 读文件（SAFE / PARALLEL）：截断长文件（头部+尾部窗口）
tools/write_file.py    # 写文件（NEVER / SEQUENTIAL）：原子写（temp+rename）
tools/run_command.py   # 执行 shell 命令（NEVER / SEQUENTIAL）：超时、输出截断、退出码进 details
```

三个工具覆盖 replay（safe vs never）与 execution（parallel vs sequential）的真实用途、错误编码不抛。

**workspace 边界的诚实声明（v2）**：路径规范化 + 前缀校验（~30 行）只约束 **read/write 的文件路径参数**；**run_command 是任意 shell，workspace 边界拦不住它**（`cat /etc/passwd` 照跑）——run_command 是已知未防护的最大信任面，教学环境使用；未来权限扩展经 before_tool_call 钩子承接。写明这点比"看似设防"诚实。

## 关键设计决策

1. **手写校验 + 显式债务**（见三段式节）：标量参数支持，复合 schema 拒绝；首个复合参数工具出现时引 jsonschema。
2. **replay 静态声明**在 ToolSpec。
3. **Protocol 而非 ABC**。
4. **details 落盘、不进上下文**：观察可重建（不变量 6）与上下文干净兼得。
5. **整批串行策略**（任一 SEQUENTIAL）：从简，避开分桶跨桶关系的正确性坑。

## 测试清单（验收标准）

1. 注册表：重名抛错；definitions 稳定排序
2. 参数校验：缺字段/类型错/多余字段 → is_error 结果带可读信息；**复合 schema 参数 → 拒绝并报"暂不支持"**
3. 三段式：before 钩子 block → is_error 且 reason 进 content；after 钩子可替换结果
4. 并发规则：全 PARALLEL 并发；含 SEQUENTIAL 整批串行按原序
5. execute 抛异常 → is_error 结果（永不向上抛）
6. cancel：执行前触发 → 即时不执行；执行中触发 → 不 kill，timeout 兜底，结果照常
7. 三个教学工具行为 + replay/execution 声明正确
8. workspace 边界：read/write 越界路径被拒；**run_command 不受边界约束（文档性断言）**
9. duration 记录进 details
10. 落盘映射：tool_call/tool_result entry 结构与 call_id 配对（并发批次乱序落盘仍正确关联）

## 分歧点（留用户决策）

**分歧 1：ToolResult 是否保留 terminate 字段**
- 评审观点（D-3）：terminate 在第一阶段是过度设计——三个教学工具没有一个会用它，"批内全部 terminate 才停"的规则测无可测；预算/取消/验证三个停止源已由 finish_turn 覆盖。建议砍掉，未来 subagent 工具出现时再加。
- 我的观点：倾向**采纳砍掉**——评审的"答不上必然问题的字段不出现"论证与准则一致，且加回来是纯增量（ToolResult 加字段 + loop 一处检查），无迁移成本。
- **默认执行：砍**。如你不同意请说。

**分歧 2：cancel 执行中语义（已按保守方案写定，确认即可）**
- 已写定：执行中不 kill 子进程、timeout 兜底（能力降级声明）。pi 的 AbortSignal 可真中断。若你认为教学需要真中断（演示"取消立竿见影"），改用 asyncio.subprocess + process.kill（+30 行复杂度）。
- **默认执行：不 kill**。如你想要演示效果更强的取消，请说。

## 评审要点（给用户）

1. 分歧 1、2 的默认方案是否确认
2. 首批三工具与落盘映射结构
