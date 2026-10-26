# 设计稿：compaction 模块（第 6 步）

状态：草案 v2（已按独立评审修订：切点孤儿规则、中文 token 估算、usage/窗口/预算三处跨稿缺口闭合；两处分歧留用户决策）。
依据：[architecture.md](../architecture.md)「压缩（两段式）」；[storage-session.md](./storage-session.md) CompactOp；pi 参考：coding-agent compaction 与 durable CompactionTask（语义相同取教学简化版）。

## 目标

实现上下文压缩：阈值触发、切点选择、两段式提交。完成后：长任务不再被 LENGTH 打死。

## 触发（compaction/__init__.py）

```python
def should_compact(context_stats: ContextStats) -> bool: ...
    """阈值：估算上下文 token > 模型窗口 - 保留量（reserve=16384，pi 默认值）。
    模型窗口来自 RuntimeConfig.context_window（新增字段，跨稿缺口 3 闭合——
    默认 128000，模型字符串不含窗口信息，配置显式给）。"""
```

**ContextStats 的 token 来源**（跨稿缺口 1 闭合）：**实测值 = 最近 assistant entry 的 content 元数据 `usage`**（loop 稿已补：ModelResponse.usage 由 loop 落盘进 assistant entry 元数据）；无 usage（如首轮）→ 字符估算。

**中文感知的字符估算**（评审 B-3 修正）：不按 pi 的"字符/4"（英文启发式，中文 1 字≈1 token，低估 4 倍会系统性延迟触发）——**估算 = CJK 字符数 + 非 CJK 字符数 / 4**（~10 行，教学项目中文为主的必要修正）。

**触发时机**：loop 步骤 1d 工具执行完之后、下一轮组装前（任务边界内，FIFO 无并发 append，摘要安全）。压缩期间的 steering 在 FIFO 排队（drain 在 1a，压缩在 1d 后——时序上 steering 等压缩完成才注入，教学规模可接受且时序清晰）。LENGTH 响应 → **阻塞式压缩**后重发一次（仍超 → error"窗口不足"）。

## 切点选择（compaction/cut.py）

```python
def select_cut_point(replayed: list[EntryRow], keep_recent_tokens: int = 20000) -> CutPoint: ...
    """从活跃叶向老走，累积估算 token 到 keep_recent 得保留段；切点候选 =
    user entry 或「其 tool_calls 已全部有结果」的 assistant entry——绝不切在
    tool_call 与其 tool_result 之间（pi 铁律）。
    孤儿防御（评审 A-1）：切点后的保留段第一个 entry 不得是 tool_result
    （其 tool_call 已被摘要段吃掉 → 投影成孤儿 tool 消息 → OpenAI 400）——
    切点候选自动满足此约束（tool_result 必须跟它的 assistant 批）。"""
```

## 两段式（compaction/run.py）

```python
async def compact(sid, session_mgr, stream_fn) -> bool:
    # 段 1（事务外，慢）：读活跃分支（含最近 compaction 边界）
    #   → select_cut_point → stream_fn(迭代式摘要：上一份摘要 + 新内容 → 更新摘要)
    # 段 2（事务内，CompactOp）：校验 active_leaf 未变（漂移→放弃 False）
    #   → 插 compaction entry（content={"summary", "first_kept"}）→ active_leaf 更新
```

**压缩重发不计 iterations**（跨稿缺口 4 闭合）：LENGTH→压缩→重发是**同一轮的重试**，budget.iterations 不变——重发仍超限才走 error。防"一次 LENGTH 多耗一轮预算"的语义失真。

压缩后重放自动生效（compaction entry 是边界）；**原始 entry 留树**可 fork 回看。

## 与 loop 的集成

```
loop 1d 后：if should_compact(stats): compact(...)（漂移忽略下轮再试）
LENGTH：compact → 重发（一次）→ 仍超 → error"窗口不足"
```

## 关键设计决策

1. 手动触发不做（阈值 + LENGTH 两条路径够）。
2. **迭代式摘要**：请求带上一份摘要（最近 compaction entry 的 content.summary——重放边界之外更老的压缩链不需要读，每份摘要已含更老信息），新摘要 = 旧 + 新进展。
3. 文件追踪不做（pi 的滚动标签，场景简单不值）。
4. 摘要请求复用 stream_fn（当前模型；未来 CompactionPolicy 模型覆盖）。
5. **切点含孤儿防御**：候选规则结构性排除孤儿 tool_result。
6. **中文感知估算**（修正 pi 的英文启发式）。

## 测试清单（验收标准）

1. 阈值：usage 实测优先；中文估算（CJK 计 1、ASCII 计 1/4）
2. 切点：绝不切在 tool_call/tool_result 之间；**保留段首 entry 永不为孤儿 tool_result**（构造含工具轮的序列断言）
3. 两段式：mock 摘要 → 落盘 → 重放 = 摘要+保留段+其后
4. 漂移放弃：段 1 期间并发 append → 段 2 失败 → 无副作用
5. 迭代摘要：第二次压缩请求含第一份摘要
6. LENGTH：压缩→重发成功；仍超→error；**重发不增 iterations**
7. 集成：长任务（mock 大 usage）全程含一次压缩 completed
8. 原始 entry 保留：history() 全可见

## 留用户决策

**分歧 6：压缩的摘要模型**
- 现方案：摘要用当前模型（同 stream_fn）——一次压缩 = 一次全价模型调用。
- 备选：spec.json 加 `compaction_model` 字段（便宜模型做摘要）——pi 的 CompactionPolicy modelOverrides 先例。
- **默认执行：当前模型**（第一阶段从简，字段是纯增量）。

## 评审要点（给用户）

1. 分歧 6；中文估算公式；切点孤儿防御的规则表述
