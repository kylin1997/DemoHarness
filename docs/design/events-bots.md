# 设计稿：events + bots 模块（第 5 步）

状态：草案 v2（已按独立评审修订；跨稿冲突已闭合：Event 形状以本稿为准并回写第 0 步，会话层模型存储显式移交第 1 步补列）。
依据：[architecture.md](../architecture.md)「bot：用户交互的个体」；[botruntime-api.md](./botruntime-api.md)（Event 形状本稿为准）；pi 参考：EventStream、pi.agent 文档。

## 目标

实现事件流（SDK 调用方的活体通道）与 bot 装配（spec → 运行实体）。

## 事件流（core/events.py）

```python
@dataclass(frozen=True)
class Event:
    task_id: str
    session_id: str          # 通用归组键（SDK 调用方/日志/测试按会话聚合都需要）
    type: str                # 见全集表
    data: dict

class EventBus:
    """内核产出事件 → publish → 订阅者各自消费。活体推送不是历史源（不变量 6）：
    断线/慢消费丢帧后，观察面靠下一个事件或主动查 storage 自愈，不补发。"""

    def subscribe(self, session_id: str | None = None,
                  task_id: str | None = None) -> "Subscription": ...
        """双维过滤（会话/任务）；Subscription 是 AsyncIterator[Event]。
        实现：每订阅者独立 asyncio.Queue（maxsize=256）；publish 对每个订阅者
        put_nowait——队列满即丢该订阅者的一帧（publisher 永不 await，扇出不阻塞）。"""
    def publish(self, event: Event) -> None: ...
```

**事件类型全集**（8 类，唯一权威在此；第 0 步门面注释已同步）：

| type | data | 产生点 |
|---|---|---|
| task_started | {input_preview, model} | loop 步骤 0 |
| text_delta | {text, final: bool} | provider：流式增量（final=false）或整段终帧（final=true，非流式过渡期） |
| tool_call_started | {call_id, name, params} | 三段式 prepare 后 |
| tool_result | {call_id, content, is_error, duration} | 三段式 finalize 后 |
| phase_changed | {phase} | 对应表各落点 |
| task_finished | {stop_reason, iterations, tools_used} | 终态落盘后 |
| steering_received | {text} | steering drain 时（task/session 归属在 Event 字段） |
| compaction_started | {summary_preview} | 压缩段 2 提交后（compaction 可观察） |

**慢消费者语义（诚实声明）**：订阅者丢帧后会停留在旧状态，**直到下一个事件到达或主动查询 task/session_tasks 刷新**；phase 类事件建议消费者用“最新帧覆盖”处理（收到即覆盖显示，不追加），丢中间帧无感知。不承诺过程帧完整。

## bot 装配（BotRuntime 层）

**spec 三文件**（编辑体验优先：markdown 人改，json 机器读）：

```text
bots/architect/SOUL.md        # 身份
bots/architect/AGENTS.md      # 行动指南
bots/architect/spec.json      # {"model": "openai/gpt-4o"(可空=继承全局), "tools": ["..."](可空=全部)}
```

**装配顺序与校验分层**（build 报语法、用时报语义）：

```
build 时：全量加载 bots/ → 每个 spec 语法校验（SOUL/AGENTS.md 存在、json 可解析、
          model 格式合法）——语法坏 → build 报错（坏 spec 拖垮 build 是合理默认，
          目录就那么几个）
bot(name) 时：名字命中校验（不存在的名字抛异常，第 0 步契约）
bot 首次使用时（run/start）：白名单语义校验（工具名在最终注册表内——
          最终注册表 = 内置工具 + RuntimeConfig.tools 注入的并集；
          注入工具同样受白名单约束，测试要用注入工具就在 spec.json 白名单里列它）
```

**system_prompt 拼接格式（唯一权威在此）**：`SOUL.md 内容 + "\n\n---\n\n" + AGENTS.md 内容`，SOUL 在前（身份先于规则）；loop 只认 `BotSpec.system_prompt` 成品字段，不自己拼。

**workspace 目录生命周期**：build 时一次性创建全部已加载 bot 的 `workspace/<bot>/`（教学规模 bot 少，无竞态、无懒建复杂度）；delete 会话**不碰** workspace 文件（文件是 bot 资产非会话资产）；bot 目录的增删是文件系统操作，不在 SDK 内。

**会话层模型（三层中间层）的存储**：**sessions 表补 `model TEXT` 列**（第 1 步 schema 的遗漏，本稿显式移交——storage-session.md 需加一列与 set 途径；fork 不继承 model、reset 保留）。SDK 会话级模型覆盖 = 写会话行 model 字段；spec.model 是 bot 级默认，会话行 model 是会话级覆盖，解析链任务 > 会话行 > spec > 全局（四层实际三级：spec 与会话行同级取后者优先）。

**title 归属**：第一阶段 title 保持 None——调用方需要显示名时用 `bot 名 + 短序号`；自动摘要标题（首条输入截断）留给后续，不引入"运行中改 title"的动词。

## 与其他模块的接口

| 模块 | 契约 |
|---|---|
| loop.py | 各阶段 publish（task_started/phase_changed/task_finished/steering_received） |
| tools.py | tool_call_started / tool_result |
| providers | text_delta（流式时增量+final 标记） |
| compaction | compaction_started |
| 门面 | TaskHandle.events() = subscribe(task_id=...)；需要会话级活体事件时可用 subscribe(session_id=...) |
| SDK | set_session_model() 写 sessions.model |

## 关键设计决策

1. **弱引用无缓冲 + 独立队列扇出**：每订阅者独立 bounded 队列，满即丢帧、publish 永不阻塞；慢消费自愈语义诚实声明（不承诺过程帧完整）。
2. **事件全集 8 类一次定死**（含 compaction_started 与 final 标记），data 键名在表中锚定。
3. **三文件 bot 目录 + 分层校验**（build 语法/名字命中/用时白名单）。
4. **system_prompt 拼接格式定死**，loop 认成品字段。
5. **workspace 生命周期一句话**：build 建全部、delete 不碰、bot 目录操作在文件系统。
6. **不做**：事件持久化、跨进程事件、事件重放调试、title 自动摘要。

## 测试清单（验收标准）

1. publish/subscribe 基本环；按 session 与 task 双维过滤
2. 扇出：两订阅者各自收到同一事件；慢订阅者队列满丢帧、快订阅者不受影响、publish 不阻塞
3. 无订阅者 publish 不报错
4. loop 集成：事件序列顺序正确（task_started → … → task_finished）
5. text_delta final 标记：非流式 provider 发整段 final=true
6. bot 装配：语法校验分层（坏 json build 报、坏工具名用时报）、白名单含注入工具（mock 流程）
7. system_prompt 拼接格式断言（SOUL+分隔符+AGENTS）
8. workspace：build 后目录存在；delete 会话后文件仍在
9. sessions.model 覆盖链：任务 > 会话行 > spec > 全局（依赖第 1 步补列）

## 留用户决策

**分歧 5：sessions 表补 model 列（会话层模型存储）**
- 本稿裁决：补列（第 1 步 schema 遗漏）。四层解析（任务>会话行>spec>全局）实质是三层取最近覆盖。
- 备选：不补列，会话级模型覆盖走"写一个 sidecar json"——脏。
- **默认执行：补列**（第 1 步 storage-session.md 同步修订）。

## 评审要点（给用户）

1. 分歧 5 的补列方案
2. 慢消费者"丢帧自愈"的诚实语义是否可接受（vs 做 phase 帧的 last-winner 保底）
3. 事件 8 类与 final 标记
