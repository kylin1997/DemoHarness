# 设计稿：loop + providers 模块（第 4 步）

状态：草案 v2（已按独立评审修订；两处分歧留用户决策，见文末。task.md 同步收敛，见修订说明）。
依据：[architecture.md](../architecture.md)「核心流程-一次任务运行」「内核与扩展的边界」；pi 参考：`agent-loop.ts`（双层循环 :172、StreamFn 契约 :390、steering 注入）；已完成模块契约：storage-session（FIFO/running）、task（phase 推导/恢复）、tools（三段式/落盘映射）。

## 目标

实现任务循环骨架（ReAct 形态）与模型接入注入点（stream_fn）+ OpenAI 兼容最小 provider。完成后：**最小 SDK 闭环可跑**（mock 模型跑通完整任务循环——第 4 步里程碑）。

## 模块收敛（v2——与 task.md 的职责重划，评审 B-1 的裁决）

**loop.py 是唯一真循环；task.py 降为纯函数库**（phase 推导 + stop_reason 落点 + 恢复扫描），不再有自己的 while 循环：

| 职责 | 归属 |
|---|---|
| while 循环、响应分类、工具编排、steering drain | **loop.py** |
| `decide(entry_prefix) -> NextStep` 纯函数 | task.py（loop 每轮调用） |
| stop_reason 六态的落点函数（写进终态 entry 元数据） | task.py 定义、loop 调用 |
| mark/clear_running 时点 | loop（见下） |
| 恢复扫描（scan_and_recover） | task.py（BotRuntime 打开存储时） |

**三相 ↔ loop 步骤 ↔ append 落点对应表**（两稿唯一权威，收敛后此处为准）：

| phase | 触发（loop 步骤） | append 落点 |
|---|---|---|
| input | loop 步骤 1（进循环前） | user entry（携带 task_id） |
| awaiting_tools | 模型返回工具调用后 | assistant entry（含 tool_calls） |
| tools_completed | 三段式执行完 | tool_result entries |
| final_response | 终答/终态 | 终态 assistant entry（content 带 stop_reason 元数据，task.py 方案 A——**由 loop 调 task.py 的落点函数写**） |

## StreamFn 协议（core/loop.py，pi 契约的 Python 化）

```python
@dataclass(frozen=True)
class ModelRequest:
    system_prompt: str          # bot 的 SOUL + AGENTS.md 合成
    messages: list[dict]        # 重放产出的上下文投影（OpenAI 格式，见投影规格）
    tools: list[ToolSpec]
    model: str                  # 三层解析后的最终模型
    max_tokens: int | None

@dataclass(frozen=True)
class ModelResponse:
    stop_reason: ModelStop      # OK / LENGTH / ERROR / ABORTED（模型层四态）
    text: str
    tool_calls: list[ToolCall]  # call_id = 模型返回的 tool_calls[].id，端到端透传
    usage: dict | None          # token 统计——loop 落盘进 assistant entry 的 content 元数据
                                # （compaction 阈值的实测来源，跨稿缺口闭合）
    error: str | None

class ModelStop(Enum):
    OK = "ok"; LENGTH = "length"; ERROR = "error"; ABORTED = "aborted"

StreamFn = Callable[[ModelRequest, asyncio.Event], ModelResponse]
    """不得抛异常——一切失败编码进 stop_reason。async 签名；cancel Event 由循环传入。
    provider 内部做 bounded retry（429/5xx/超时各重试 1-2 次，~10 行）——重试是长程任务的
    必然设施（一次网络抖动≠整个任务清零），与不抛契约不冲突（最终失败才编码 ERROR）。"""
```

**与任务六态的映射**（loop 翻译，provider 不知情）：OK+无工具→completed / OK+工具→继续 / LENGTH→error（带"上下文超限"诊断；第 6 步压缩落地后改走压缩路径；**过渡期教学兜底**：引导用 reset 到边界 entry 手动续，而非删会话重来）/ ERROR→error / ABORTED→cancelled / 空响应→empty_response。

## 循环骨架（core/loop.py）

```python
async def run_task_loop(sid, task_id, input_text, runtime: LoopRuntime) -> TaskResult:
```

```
0. 输入 append（user entry，task_id）→ mark_running(sid, task_id)（第 1 步契约：
   输入 append 提交后登记——挂点唯一：loop 步骤 0，早于循环）
1. while True:
   a. steering drain：await drain_pending(sid) —— 等待并执行本会话 FIFO 里排队的
      steering append 操作（FIFO 顺序即注入顺序，天然去重）。
      ⚠ 澄清 v1 的错误论断：steering 仍需要一个信号（FIFO await），只是从 pi 的
      回调形式换成了"循环每轮边界 drain FIFO"；重放自然携带的部分只省掉了
      "steering 消息单独存储"——信号没有消失（评审 A-1）。
   b. 组装上下文：replay_entries → OpenAI 投影（规格见下）→ ModelRequest
   c. stream_fn(request, cancel_event)
   d. 响应分类（映射表）：
      - OK+tool_calls：append assistant entry（tool_calls，phase=awaiting_tools）
        → execute_tool_calls（第 3 步）→ append tool_result entries（phase=tools_completed，
          loop 执行落盘映射）→ budget.iterations+=1
          超限 → 收尾尝试（见下）→ cancel 检查 → 继续 1a
      - OK 纯文本：append 终态 assistant entry（stop_reason 元数据，task.py 落点函数）
        → finish_turn 决策（默认 end）→ break
   e. 事件推送（第 5 步前用日志占位）
2. finally: clear_running **仅当终态已落盘**；异常路径先物化 interrupted 终态
   （调 task.py 恢复物化函数，恰好一次）再 clear——running 守卫不变量
   （"非空 ⟺ 输入已落盘、终态未落盘"）在异常路径不破（评审 B-2）。
```

**预算收尾尝试**（v2 写死失败路径）：达 max_iterations → 发一次"直接作答勿用工具"请求 → **模型若仍返回工具调用：丢弃工具调用、取文本作答；文本为空 → max_iterations**（不是 empty_response——预算耗尽是真相；与 pi 的 LENGTH 截断判失败是两回事，不混用）。

## 上下文投影规格（v2 补齐——评审 A-2，否则 OpenAI 400）

```python
def project_context(replayed: list[EntryRow]) -> list[dict]:
    """entry 树重放结果 → OpenAI messages。规则：
    - user entry → {"role": "user", "content": ...}
    - assistant entry：文本 + tool_calls → 一条 {"role": "assistant", "content": text,
      "tool_calls": [{"id": call_id, "type": "function",
      "function": {"name": name, "arguments": json.dumps(params)}}, ...]}
      （一条 assistant 消息携带本批全部 tool_calls，保持模型给出的顺序）
    - tool_result entry → {"role": "tool", "tool_call_id": call_id, "content": content}
      （每个结果一条独立 tool 消息；乱序落盘的并发批次按对应 assistant 的
      tool_calls 顺序重排——call_id 配对，不靠相邻顺序）
    - compaction entry → {"role": "user", "content": 摘要}（或 system 拼接——实现期定）
    - reset entry → 跳过（边界后的新起点，见第 1 步重放）
    - interrupted entry → {"role": "tool", "content": "Task interrupted"} 终结开口的调用链
    """
```

**call_id 端到端透传**：`call_id` = 模型返回的 `tool_calls[].id`，provider 解析保留、loop 落盘原样写、投影原样回——OpenAI 校验天然满足，我们不自造 id。

## OpenAI 兼容 provider（providers/openai.py）

```python
def make_openai_stream_fn(api_key: str, base_url: str | None = None) -> StreamFn:
    """~150-180 行：httpx async 客户端调 /chat/completions（非流式第一版），
    解析 tool_calls（保留模型 id）、JSON 字符串参数往返（loads/dumps），
    bounded retry（429/5xx/超时 ×2），错误全部编码进 ModelResponse。
    httpx 而非 urllib：async 原生 + 第 5 步流式直接 client.stream()；
    urllib 同步阻塞会卡死单进程事件循环 30 秒（评审 C-3）——网络栈是刚需依赖，
    零依赖原则不适用于此。"""
```

## 与其他模块的接口

| 模块 | 契约 |
|---|---|
| task.py | loop 是唯一循环；调 decide/落点函数/恢复物化（对应表为准） |
| tools.py | execute_tool_calls + definitions + 落盘映射执行 |
| storage/session | submit（一切 append 经 FIFO）；replay_entries；drain_pending（新增原语：等 FIFO 空并执行排队 steering） |
| events（第 5 步） | 循环各阶段发事件 |
| compaction（第 6 步） | LENGTH 后续处理位 |

**连续对话的模型解释**（教学关键句，v2 补）：一次 run 一个 Task；连续对话 = 新 run（新 Task）**从同一 session 的活跃叶重放**——上下文自然连续，Task 只是"这一轮运行"的状态包装，不是上下文清零。

## 关键设计决策

1. **steering = 迭代边界的 FIFO drain**（v2 修正表述）：FIFO await 是信号（没消失）；数据形态的收益是 steering 落盘后重放自然携带、无需单独存储。入站分发（running→steering / 已结束→follow-up 新 run）由门面层判断（第 0 步 API 契约）。
2. **单层循环**：一次 run 一个输入；连续对话新 run 同 session 重放（解释句见上）。
3. **ModelStop 四态与任务六态分离**。
4. **预算（硬边界）与 finish_turn（策略边界）分开**。
5. **非流式第一版**，协议不变实现升级（第 5 步加 streaming）。
6. **provider 内 bounded retry**（v2 采纳评审 C-1）：长程任务必然设施，不是第 6 步的事。
7. **httpx**（v2 定）：async 刚需，urllib 阻塞事件循环。
8. **loop/task 职责重划**（对应表为准）：loop 唯一循环，task 纯函数库。

## 测试清单（验收标准）

1. mock stream_fn 跑通：输入→2 轮工具→终答 completed（里程碑）
2. 投影规格：单批多工具 + **乱序落盘** → 投影按 assistant.tool_calls 顺序重排（call_id 配对）
3. call_id 透传：模型 id 落盘、投影回传一致（不重命名）
4. LENGTH→error 诊断；ERROR→error；ABORTED→cancelled；空→empty_response
5. 预算：超限→收尾成功 completed；收尾返回工具调用→丢弃取文本；文本空→max_iterations
6. cancel：工具前/中触发行为符合 tools.md 契约
7. steering drain：运行中 steer → entry 落盘 → 下轮上下文含它（mock 断言收到）
8. running 异常路径：loop 中途抛异常 → interrupted 物化 → running 清空 → 恢复扫描不再重复物化
9. 门面闭环：build(mock) → bot.run() → TaskResult（第 0 步最短路径）

## 分歧点（留用户决策）

**分歧 3：LENGTH 过渡期的处理**
- 现方案：LENGTH→error 终态 + 教学兜底话术（引导 reset 续跑）。理由：语义诚实（压缩未实现就是不可继续），第 6 步改走压缩路径。
- 备选：LENGTH → 自动触发"手动压缩"（同步截断丢旧 entry + 摘要占位）——演示效果更好但引入一个第 6 步会推翻的临时实现。
- **默认执行：error + reset 兜底**。若你更在意第 4-5 步演示动线，请说。

**分歧 4：finish_turn 挂点的现阶段形态**
- 现方案：只留协议签名（Callable），第一阶段唯一实现是默认 end——验证器扩展（经 finish_turn 消费证据）连示例都不给。
- 备选：给一个教学示例 finish_turn（如"检查任务文本里是否含完成标记"），让"验证不凭模型自称"有个可演示的最小实现。
- **默认执行：只留签名**。若你想让"结果验证"在第 4 步就有演示，请说。

## 评审要点（给用户）

1. 分歧 3、4 的默认方案
2. loop/task 重划（对应表）与投影规格
3. 连续对话的教学解释句
