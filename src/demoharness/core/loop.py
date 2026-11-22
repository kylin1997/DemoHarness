"""DemoHarness 内核 · 任务循环骨架（core/loop.py）。

**唯一的真循环**（task.py 是纯函数库，见 loop-providers.md「模块收敛」）。
ReAct 形态：思考 → 工具 → 观察 → 再判断，直到终态。

注入点契约（架构「内核与扩展的边界」）：
- stream_fn：调用模型的函数，每轮调用一次，**不得抛异常**（一切失败编码进
  ModelResponse.stop_reason）。providers/ 是具体实现。
- finish_turn：每轮结束时的决策函数，返回 VerifyVerdict(passed, reason,
  evidence)；**结果验证在此消费证据**——验证器是扩展（strategies/verification.py），
  由 BotRuntime 装配期注入同一个实例。只有 passed=True 才允许 completed；
  验证失败不新增第七态，而是把失败原因落为 verification_failed 证据 entry，
  下一轮重放自然携带 → 模型自纠 → 再次验证 → 达 max_verify_retries 以
  StopReason.ERROR 收尾（六态闭表不变）。
- transform_context：组上下文前的裁剪注入点（compaction/ 第 6 步挂接）。
- before/after 工具钩子经 tools.py 三段式，loop 不直接管权限。

三相 ↔ loop 步骤 ↔ append 落点（唯一权威 = loop-providers.md 对应表）：
  input → 进循环前 append user entry（task_id）→ mark_running
  awaiting_tools → 模型返回工具调用 → append tool_call entries
  tools_completed → 三段式执行完 → append tool_result entries
  final_response → 终答 → append 终态 assistant entry（content 带 stop_reason）

stop_reason 六态触发源：completed=模型无工具调用且验证通过 / error=stream_fn
编码错误或验证失败达上限 / max_iterations=预算 / cancelled=信号 /
empty_response=模型空返回 / interrupted=恢复物化专属。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .events import (
    PHASE_CHANGED,
    TASK_FINISHED,
    TASK_STARTED,
    Event,
    EventBus,
    TaskPhase,
    text_delta_event,
)
from .session import (
    VERIFICATION_FAILED_KIND,
    AppendOp,
    SessionManager,
    compact_groups,
    message_groups,
)
from .storage import EntryRow, HistoryEntry, TaskStore
from .task import (
    TOOL_RESULT_KIND,
    Action,
    ActionState,
    CheckEvidence,
    Delivery,
    Run,
    RunReason,
    StopReason,
    TaskState,
    Verdict,
    materialize_interrupted,
    terminal_content,
)
from .tools import (
    BeforeToolHook,
    RecoverableTools,
    ToolCall,
    ToolRegistry,
    ToolRejected,
    ToolResult,
    ToolSpec,
    UnknownEffect,
    execute_tool_calls,
    model_tool_call,
)


class ModelStop(Enum):
    """模型层四态（provider 报给 loop，loop 翻译成任务六态）。"""

    OK = "ok"
    LENGTH = "length"
    ERROR = "error"
    ABORTED = "aborted"


@dataclass(frozen=True)
class ModelRequest:
    system_prompt: str
    messages: list[dict]
    tools: list[ToolSpec]
    model: str
    max_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    stop_reason: ModelStop
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict | None = None
    error: str | None = None


StreamFn = Callable[[ModelRequest, asyncio.Event], "asyncio.Coroutine[Any, Any, ModelResponse]"]


@dataclass(frozen=True)
class VerifyVerdict:
    """finish_turn 的裁决（公开类型，唯一定义处）。

    - passed：是否允许以 completed 收尾（不凭模型自称，要消费证据）。
    - reason：失败原因，落进 verification_failed 证据 entry 与 TaskResult.error。
    - evidence：可机械判定的结构化证据（数量/配对/命中项等），供 SDK 与测试断言。
    """

    passed: bool
    reason: str = ""
    evidence: dict = field(default_factory=dict)


FinishTurn = Callable[["LoopContext"], VerifyVerdict]
# 上下文裁剪注入点：entry 流水 -> 裁剪后的 entry 流水（压缩扩展第 6 步实现）。
TransformContext = Callable[[list[EntryRow]], list[EntryRow]]


@dataclass(frozen=True)
class ToolCallRaw:
    """模型返回的工具调用（call_id = 模型 tool_calls[].id 端到端透传）。"""

    call_id: str
    name: str
    arguments: str  # JSON 字符串，loop 负责 loads 成 params


@dataclass
class LoopContext:
    """loop 每轮结束时暴露给 finish_turn 的决策上下文。"""

    session_id: str
    task_id: str
    entry_prefix: list[EntryRow]
    iterations: int
    tools_used: int
    last_text: str
    last_tool_calls: list[ToolCallRaw]
    # 本轮上下文中的全部 tool_result entry（取自 entry_prefix，即 entry 流水）。
    # 历史事实只来自 entry（架构不变量 6），验证器据此机械配对与判定。
    last_tool_results: list[EntryRow] = field(default_factory=list)


@dataclass
class LoopRuntime:
    """loop 需要的全部注入依赖（BotRuntime 装配期拼装，唯一构造路径）。"""

    session_mgr: SessionManager
    stream_fn: StreamFn
    registry: ToolRegistry
    event_bus: EventBus
    system_prompt: str
    model: str
    max_iterations: int
    context_window: int
    finish_turn: FinishTurn
    cancel: asyncio.Event | None = None
    # 工具执行前的钩子（before_tool_call）：约束扩展经此注入，按顺序遍历。
    before_hooks: list[BeforeToolHook] = field(default_factory=list)
    # 上下文裁剪注入点（compaction/ 第 6 步挂接；本轮冻结签名，默认不裁剪）。
    transform_context: TransformContext | None = None
    # 压缩扩展（第 6 步）：loop 只调用其 should_compact_for / compact 方法，
    # 不反向 import compaction（鸭子类型，避免环）。
    compaction: Any | None = None
    # 验证失败的有界纠正回路上限（达到即以既有 ERROR 终态收尾，不无限重试）。
    max_verify_retries: int = 3


@dataclass(frozen=True)
class TaskResult:
    stop_reason: StopReason
    content: str
    error: str | None
    task_id: str
    session_id: str
    iterations: int
    tools_used: int


def _assistant_content(text: str, tool_calls: list[dict]) -> str:
    """assistant entry 的 content：纯文本或带 tool_calls 元数据（投影用）。"""
    if not tool_calls:
        return text
    return json.dumps({"text": text, "tool_calls": tool_calls}, ensure_ascii=False)


# ── 上下文投影：entry 树重放结果 → OpenAI messages ──


def project_context(replayed: list[EntryRow]) -> list[dict]:
    """entry 树重放结果 → OpenAI messages（loop-providers.md 投影规格）。

    - user entry → {"role": "user"}
    - assistant entry：文本 + tool_calls → 一条 assistant 消息携带本批全部
      tool_calls（保持模型给出的顺序）
    - tool_call entry → 跳过（tool_call 是内部落盘映射，不直接进 OpenAI；
      assistant 消息里的 tool_calls 元数据才是模型看到的请求）
    - tool_result entry → {"role": "tool", "tool_call_id": call_id}；**并有
      tool_calls 的 assistant 之后紧邻的 tool_result 组按 assistant 声明的
      tool_calls 顺序重排**（乱序落盘的并发批次靠 call_id 配对，不靠相邻顺序）
    - compaction entry → {"role": "user", "content": 摘要}
    - reset entry → 跳过（边界后的新起点）
    - interrupted entry → {"role": "tool", "content": "Task interrupted"}
    - verification_failed entry → {"role": "user", "content": 失败原因与重试提示}
      （验证纠正回路：证据落盘后必须能进入下一轮模型上下文，模型才可能自纠）
    """
    messages: list[dict] = []
    i = 0
    n = len(replayed)
    while i < n:
        e = replayed[i]
        if e.kind == "user":
            messages.append({"role": "user", "content": _content_text(e.content)})
            i += 1
        elif e.kind == "assistant":
            content = _parse(e.content)
            if isinstance(content, dict) and content.get("tool_calls"):
                tool_calls = content["tool_calls"]
                messages.append(
                    {
                        "role": "assistant",
                        "content": content.get("text", ""),
                        "tool_calls": [
                            _to_openai_tool_call(tc) for tc in tool_calls
                        ],
                    }
                )
                # 收集紧跟其后的一串 tool_result（直到遇到非 tool_result），
                # 按 tool_calls 声明顺序重排。
                j = i + 1
                results_by_id: dict[str, dict] = {}
                while j < n and replayed[j].kind == "tool_result":
                    c = _parse(replayed[j].content)
                    if isinstance(c, dict) and c.get("call_id"):
                        results_by_id[c["call_id"]] = {
                            "role": "tool",
                            "tool_call_id": c["call_id"],
                            "content": c.get("content", ""),
                        }
                    j += 1
                for tc in tool_calls:
                    cid = tc.get("call_id")
                    if cid in results_by_id:
                        messages.append(results_by_id[cid])
                i = j
            else:
                messages.append({"role": "assistant", "content": _content_text(e.content)})
                i += 1
        elif e.kind == "tool_result":
            # 孤立 tool_result（无前置 assistant 声明）——保守按原序发
            c = _parse(e.content)
            if isinstance(c, dict):
                messages.append(
                    {"role": "tool", "tool_call_id": c.get("call_id"), "content": c.get("content", "")}
                )
            i += 1
        elif e.kind == "compaction":
            messages.append({"role": "user", "content": _content_text(e.content)})
            i += 1
        elif e.kind == "interrupted":
            messages.append({"role": "tool", "content": "Task interrupted"})
            i += 1
        elif e.kind == VERIFICATION_FAILED_KIND:
            messages.append({"role": "user", "content": _verification_hint(e.content)})
            i += 1
        else:
            # reset / tool_call：跳过
            i += 1
    return messages


def _verification_hint(content: str) -> str:
    """把验证失败证据 entry 投影成给模型的自纠提示（不吞掉失败原因）。"""
    c = _parse(content)
    if not isinstance(c, dict):
        return f"【验证未通过】{content}；请补齐证据后重新作答。"
    attempt = c.get("attempt", "?")
    limit = c.get("max_verify_retries", "?")
    reason = c.get("reason", "")
    return f"【验证未通过·第 {attempt}/{limit} 次】{reason}；请补齐证据后重新作答。"


def _parse(content: str) -> dict | str:
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return content
    return parsed if isinstance(parsed, dict) else content


def _content_text(content: str) -> str:
    c = _parse(content)
    if isinstance(c, dict):
        return c.get("text", "")
    return c


# ── 循环骨架 ──


async def run_task_loop(
    sid: str,
    task_id: str,
    input_text: str,
    runtime: LoopRuntime,
) -> TaskResult:
    """执行一次任务的完整 ReAct 循环。

    约定：门面（BotRuntime）在进本循环前已 append user entry（携带 task_id）
    并 mark_running(sid, task_id)。本函数从步骤 1（while 循环）开始。

    异常路径：finally 里**先物化 interrupted 再 clear_running**——running
    守卫不变量（非空 ⟺ 输入已落盘、终态未落盘）在异常路径不破。
    """
    cancel = runtime.cancel or asyncio.Event()
    iterations = 0
    tools_used = 0
    verify_failures = 0

    runtime.event_bus.publish(
        Event(
            task_id=task_id,
            session_id=sid,
            type=TASK_STARTED,
            data={"input_preview": input_text[:120], "model": runtime.model},
        )
    )

    try:
        while True:
            # a. steering drain：steering 的活体事件由门面 TaskHandle.steer
            #    publish；user entry 已在会话 FIFO 落盘，重放自然携带。
            await _drain_steering(runtime, task_id, sid)

            # a2. 取消/预算是硬边界：信号已置则本轮起不再调用 stream_fn 与工具。
            if cancel.is_set():
                return await _finish(
                    runtime, sid, task_id, "", StopReason.CANCELLED, iterations, tools_used
                )

            # a3. 压缩检查（第 6 步）：每轮组装前判定阈值，超限则两段式压缩；
            #     压缩后下一轮从新活跃叶重放，本循环自然用压缩后的上下文。
            if runtime.compaction is not None:
                _pre = await _replay_prefix(runtime.session_mgr, sid)
                if runtime.compaction.should_compact_for(_pre):
                    await runtime.compaction.compact(sid, _pre)

            # b. 组装上下文（从活跃叶重放 → 注入点裁剪 → OpenAI 投影）
            prefix, messages = await _context_messages(runtime, sid)
            request = ModelRequest(
                system_prompt=runtime.system_prompt,
                messages=messages,
                tools=runtime.registry.definitions(),
                model=runtime.model,
                max_tokens=None,
            )

            # c. 调用模型（stream_fn 不得抛异常）
            resp = await runtime.stream_fn(request, cancel)

            # c2. 文本活体事件：非流式 provider 一整段即 final=true 终帧。
            if resp.text:
                runtime.event_bus.publish(text_delta_event(task_id, sid, resp.text, True))

            # d. 响应分类 → 映射到任务六态
            if resp.stop_reason == ModelStop.ABORTED:
                return await _finish(runtime, sid, task_id, "", StopReason.CANCELLED, iterations, tools_used)
            if resp.stop_reason == ModelStop.ERROR:
                return await _finish(
                    runtime, sid, task_id, "", StopReason.ERROR, iterations, tools_used,
                    error=resp.error or "模型调用错误",
                )
            if resp.stop_reason == ModelStop.LENGTH:
                # 压缩扩展：LENGTH → 压缩后重发一次（同轮重试，不计 iterations）；
                # 压缩成功则 continue 回循环顶、用压缩后上下文再调模型，仍超才 error。
                if runtime.compaction is not None:
                    _pre = await _replay_prefix(runtime.session_mgr, sid)
                    if await runtime.compaction.compact(sid, _pre):
                        continue
                return await _finish(
                    runtime, sid, task_id, "", StopReason.ERROR, iterations, tools_used,
                    error="上下文超限（LENGTH）",
                )
            if not resp.text and not resp.tool_calls:
                return await _finish(runtime, sid, task_id, "", StopReason.EMPTY_RESPONSE, iterations, tools_used)

            if resp.tool_calls:
                await _append(runtime, sid, "assistant", _assistant_content(resp.text, resp.tool_calls))
                runtime.event_bus.publish(
                    Event(task_id=task_id, session_id=sid, type=PHASE_CHANGED,
                          data={"phase": TaskPhase.AWAITING_TOOLS})
                )
                from .tools import ToolCall

                raw = [_to_raw(c) for c in resp.tool_calls]
                calls = [ToolCall(call_id=c.call_id, name=c.name, params=_loads(c.arguments)) for c in raw]
                for c in calls:
                    await _append(runtime, sid, "tool_call", json.dumps({"call_id": c.call_id, "name": c.name, "params": c.params}))
                    runtime.event_bus.publish(
                        Event(task_id=task_id, session_id=sid, type="tool_call_started",
                              data={"call_id": c.call_id, "name": c.name, "params": c.params})
                    )
                results = await execute_tool_calls(
                    calls, runtime.registry, cancel=cancel, before_hooks=runtime.before_hooks
                )
                for tc, tr in zip(calls, results):
                    await _append(
                        runtime,
                        sid,
                        "tool_result",
                        json.dumps({"call_id": tc.call_id, "content": tr.content, "is_error": tr.is_error, "details": tr.details}),
                    )
                    runtime.event_bus.publish(
                        Event(task_id=task_id, session_id=sid, type="tool_result",
                              data={"call_id": tc.call_id, "content": tr.content, "is_error": tr.is_error,
                                    "duration": (tr.details or {}).get("duration")})
                    )
                    tools_used += 1
                runtime.event_bus.publish(
                    Event(task_id=task_id, session_id=sid, type=PHASE_CHANGED,
                          data={"phase": TaskPhase.TOOLS_COMPLETED})
                )
                iterations += 1
                if iterations >= runtime.max_iterations:
                    return await _budget_finish(runtime, sid, task_id, iterations, tools_used, cancel)
                continue

            # e. 纯文本（无工具调用）：finish_turn 决策——结果验证在此消费证据。
            #    只有验证通过才允许 completed；不凭模型自称「已完成」。
            ctx = LoopContext(
                session_id=sid,
                task_id=task_id,
                entry_prefix=prefix,
                iterations=iterations,
                tools_used=tools_used,
                last_text=resp.text,
                last_tool_calls=[],
                last_tool_results=_tool_results_of(prefix),
            )
            verdict = _coerce_verdict(runtime.finish_turn(ctx))
            if verdict is None or verdict.passed:
                return await _finish(
                    runtime, sid, task_id, resp.text, StopReason.COMPLETED, iterations, tools_used
                )

            # 验证失败：不新增第七态——失败原因落为证据 entry，下一轮重放自然
            # 携带（project_context 投影为自纠提示）→ 模型自纠 → 再次验证；
            # 达到 max_verify_retries 以既有 ERROR 终态收尾，重试有终止条件。
            verify_failures += 1
            await _append(
                runtime,
                sid,
                VERIFICATION_FAILED_KIND,
                json.dumps(
                    {
                        "reason": verdict.reason,
                        "evidence": verdict.evidence,
                        "attempt": verify_failures,
                        "max_verify_retries": runtime.max_verify_retries,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
            if verify_failures >= runtime.max_verify_retries:
                return await _finish(
                    runtime, sid, task_id, resp.text, StopReason.ERROR, iterations, tools_used,
                    error=f"验证未通过（{verify_failures} 次）：{verdict.reason}",
                )
            iterations += 1
            if iterations >= runtime.max_iterations:
                return await _budget_finish(runtime, sid, task_id, iterations, tools_used, cancel)
            continue
    except BaseException:
        # 异常路径：物化中断（恰好一次），不静默吞异常——物化后重新抛出，
        # 让门面知晓任务异常中止。
        try:
            await materialize_interrupted(runtime.session_mgr, sid)
        finally:
            runtime.session_mgr.clear_running(sid)
        raise
    finally:
        runtime.session_mgr.clear_running(sid)


def _coerce_verdict(value: VerifyVerdict | bool | None) -> VerifyVerdict | None:
    """归一化 finish_turn 返回值，返回 None 表示「无验证裁决」。

    注入点向后兼容：第一阶段骨架测试里 `finish_turn=lambda ctx: False` 这类
    旧式 bool 占位不参与新的验证协议（它没有证据、也没有失败原因），因此
    保持其原有「纯文本即终态」语义，契约升级不破坏既有回归。生产路径由
    BotRuntime 装配的 TeachingVerifier 返回 VerifyVerdict，一律参与决策。
    """
    if isinstance(value, VerifyVerdict):
        return value
    return None


def _tool_results_of(prefix: list[EntryRow]) -> list[EntryRow]:
    """从 entry 流水中取出全部 tool_result entry（验证器的证据来源）。"""
    return [e for e in prefix if e.kind == TOOL_RESULT_KIND]


async def _context_messages(runtime: LoopRuntime, sid: str) -> tuple[list[EntryRow], list[dict]]:
    """重放 → transform_context 注入点 → 投影，返回 (证据流水, OpenAI messages)。"""
    prefix = await _replay_prefix(runtime.session_mgr, sid)
    if runtime.transform_context is not None:
        prefix = runtime.transform_context(prefix)
    return prefix, project_context(prefix)


async def _budget_finish(
    runtime: LoopRuntime, sid: str, task_id: str, iterations: int, tools_used: int, cancel: asyncio.Event
) -> TaskResult:
    """预算耗尽：发一次「直接作答勿用工具」，模型仍返回工具调用则丢弃取文本；
    文本空 → max_iterations。取消优先于预算，且取消后不再发生新的模型调用。
    """
    if cancel.is_set():
        return await _finish(runtime, sid, task_id, "", StopReason.CANCELLED, iterations, tools_used)
    _prefix, messages = await _context_messages(runtime, sid)
    request = ModelRequest(
        system_prompt=runtime.system_prompt,
        messages=messages,
        tools=[],
        model=runtime.model,
        max_tokens=None,
    )
    resp = await runtime.stream_fn(request, cancel)
    text = resp.text
    if resp.tool_calls and not text:
        text = ""
    if not text:
        return await _finish(runtime, sid, task_id, "", StopReason.MAX_ITERATIONS, iterations, tools_used, error="预算耗尽")
    return await _finish(runtime, sid, task_id, text, StopReason.MAX_ITERATIONS, iterations, tools_used)


async def _finish(
    runtime: LoopRuntime,
    sid: str,
    task_id: str,
    text: str,
    stop_reason: StopReason,
    iterations: int,
    tools_used: int,
    error: str | None = None,
) -> TaskResult:
    """落下终态 assistant entry（stop_reason 元数据）并返回 TaskResult。"""
    await _append(runtime, sid, "assistant", terminal_content(text, stop_reason))
    runtime.event_bus.publish(
        Event(
            task_id=task_id,
            session_id=sid,
            type=TASK_FINISHED,
            data={"stop_reason": stop_reason.value, "iterations": iterations, "tools_used": tools_used},
        )
    )
    return TaskResult(
        stop_reason=stop_reason,
        content=text,
        error=error,
        task_id=task_id,
        session_id=sid,
        iterations=iterations,
        tools_used=tools_used,
    )


def _to_raw(tool_call: dict) -> ToolCallRaw:
    return ToolCallRaw(
        call_id=tool_call["call_id"],
        name=tool_call["name"],
        arguments=json.dumps(tool_call.get("params", {})),
    )


def _to_openai_tool_call(tool_call: dict) -> dict:
    """把内部 dict 形式 {"call_id","name","params"} 转成 OpenAI wire 形式。"""
    return model_tool_call(tool_call.get("call_id"), tool_call.get("name"), tool_call.get("params", {}))


async def _append(runtime: LoopRuntime, sid: str, kind: str, content: str) -> str:
    return await runtime.session_mgr.submit(sid, AppendOp(sid, kind, content, parent=None))


async def _replay_prefix(session_mgr: SessionManager, sid: str) -> list[EntryRow]:
    from .session import HistoryOp

    return await session_mgr.submit(sid, HistoryOp(sid))


async def _drain_steering(runtime: LoopRuntime, task_id: str, sid: str) -> None:
    """no-op：steering 语义已由「FIFO + 重放」间接承担，无需显式 drain。

    steering（运行中追加的 user 指令）经 TaskHandle.steer() → session submit
    落盘为 user entry，并在门面 publish steering_received；loop 每轮开头
    `_replay_prefix` 从 storage 重放活跃链，该 entry 自然进入下一轮模型上下文。
    这里没有单独的「drain」动作要做——不存在「排队中的 steering 等待被消费」。
    """
    return


def _loads(s: str) -> dict:
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return {} if not s else {"_raw": s}


@dataclass(frozen=True)
class RoleResult:
    verdict: Verdict
    detail: str = ""
    checks: tuple[CheckEvidence, ...] = ()
    delivery: Delivery | None = None


@dataclass(frozen=True)
class LoopRole:
    complete: StreamFn
    model: str
    system_prompt: str
    tools: RecoverableTools
    finish: Callable[[str], RoleResult]
    max_tokens: int | None
    context_messages: int
    context_note: Callable[[], str] | None = None
    model_limit: int | None = None


def _calls(response: ModelResponse) -> list[ToolCall]:
    result = []
    ids = set()
    for raw in response.tool_calls:
        call_id, name, params = raw.get("call_id"), raw.get("name"), raw.get("params", {})
        if not isinstance(call_id, str) or not call_id or call_id in ids or not isinstance(name, str) or not isinstance(params, dict):
            raise ValueError("Model returned a malformed or duplicate tool call")
        json.dumps(params, allow_nan=False)
        ids.add(call_id)
        result.append(ToolCall(call_id, name, params))
    return result


def _budget_wait(store: TaskStore, run: Run, resource: str) -> None:
    store.end(run, TaskState.WAITING, RunReason.BUDGET_EXHAUSTED, f"Budget exhausted for {resource}")


async def _advance_action(
    store: TaskStore, run: Run, tools: RecoverableTools, action: Action, cancel: asyncio.Event,
) -> bool:
    try:
        if action.state is ActionState.STARTED:
            result = tools.reconcile(action)
            if result is not None:
                store.result(run, action, result)
                return True
            tools.authorize(action)
            intent = action.intent
        else:
            intent = tools.prepare(action)
        if not store.start_action(run, action, intent):
            _budget_wait(store, run, "tools")
            return False
        result = await tools.execute(action, intent, cancel)
    except UnknownEffect as error:
        store.end(run, TaskState.WAITING, RunReason.UNKNOWN_EFFECT, str(error))
        return False
    except (ToolRejected, OSError, UnicodeError) as error:
        result = ToolResult(str(error), is_error=True)
    store.result(run, action, result)
    return True


def _proposal(entries: list[HistoryEntry]) -> HistoryEntry | None:
    for entry in reversed(entries):
        if entry.kind == "assistant":
            return entry if entry.data["proposal"] else None
        if entry.kind in {"input", "verification_failed", "completed"}:
            break
    return None


def _verify(store: TaskStore, run: Run, role: LoopRole, proposal: HistoryEntry) -> bool:
    attempt = store.begin(run, "verifications", "verification_started", proposal_id=proposal.id)
    if attempt is None:
        _budget_wait(store, run, "verification")
        return False
    verdict = role.finish(proposal.data["text"])
    if store.task(run.task_id).state is not TaskState.RUNNING:
        return False
    if run.delegation_id is None and store.latest_input(run.task_id) > proposal.id:
        store.record(run, "verification_superseded", proposal_id=proposal.id, checks=[asdict(c) for c in verdict.checks])
        return True
    if verdict.verdict is Verdict.PASSED:
        store.end(run, TaskState.COMPLETED, RunReason.COMPLETED, delivery=verdict.delivery, checks=verdict.checks)
    elif verdict.verdict is Verdict.UNVERIFIABLE:
        store.end(run, TaskState.WAITING, RunReason.UNVERIFIABLE, verdict.detail, checks=verdict.checks)
    elif not store.task(run.task_id).budget.verifications:
        store.end(run, TaskState.FAILED, RunReason.VERIFICATION_FAILED, verdict.detail, checks=verdict.checks)
    else:
        store.record(run, "verification_failed", detail=verdict.detail, checks=[asdict(x) for x in verdict.checks])
        return True
    return False


def _length_failures(entries: list[HistoryEntry]) -> list[int]:
    failures = []
    for entry in reversed(entries):
        if entry.kind == "model_result":
            if entry.data["stop"] != ModelStop.LENGTH.value:
                break
            failures.append(entry.id)
    return failures


def _context(
    store: TaskStore, run: Run, entries: list[HistoryEntry], actions: list[Action], limit: int, length_id: int | None,
) -> list[dict] | None:
    latest = next((entry.data for entry in reversed(entries) if entry.kind == "summary"), {})
    past = latest.get("text", "")
    groups = message_groups([entry for entry in entries if entry.id > latest.get("through", 0)], actions)
    force = length_id is not None and latest.get("length_id") != length_id
    compacted = compact_groups(groups, limit, force)
    if compacted:
        through, excerpt = compacted
        summary = "\n".join(part for part in (past, excerpt) if part)[-2000:]
        changed = store.begin(run, "compactions", "summary", through=through, text=summary, length_id=length_id)
        if changed:
            past = summary
            groups = [group for group in groups if group.through > through]
        elif force:
            return None
    elif force:
        return None
    messages = [{"role": "user", "content": f"Earlier Session summary:\n{past}"}] if past else []
    messages.extend(message for group in groups for message in group.messages)
    return messages


async def run_loop(store: TaskStore, run: Run, role: LoopRole, cancel: asyncio.Event) -> None:
    while not cancel.is_set() and store.task(run.task_id).state is TaskState.RUNNING:
        actions = store.actions(run.task_id, delegation_id=run.delegation_id)
        action = next((action for action in actions if action.state is not ActionState.RESULT), None)
        if action is not None:
            if (run.delegation_id is None and action.state is ActionState.PENDING
                    and store.latest_input(run.task_id) > action.response_id):
                store.result(run, action, ToolResult("Skipped because the user added new input", is_error=True))
                continue
            if not await _advance_action(store, run, role.tools, action, cancel):
                return
            continue
        entries = store.history(run.task_id, delegation_id=run.delegation_id)
        proposal = _proposal(entries)
        if proposal is not None:
            if not _verify(store, run, role, proposal):
                return
            continue
        if role.model_limit is not None and sum(entry.kind == "model_started" for entry in entries) >= role.model_limit:
            _budget_wait(store, run, "delegated model calls")
            return
        lengths = _length_failures(entries)
        if len(lengths) > 1:
            store.end(run, TaskState.FAILED, RunReason.MODEL_LENGTH, "Model exceeded its length limit after one retry")
            return
        messages = _context(store, run, entries, actions, role.context_messages, lengths[0] if lengths else None)
        if messages is None:
            store.end(run, TaskState.FAILED, RunReason.MODEL_LENGTH, "Context cannot be compacted further within budget")
            return
        note = role.context_note() if role.context_note else ""
        instructions = "\n\n".join(filter(None, (role.system_prompt, note)))
        request = ModelRequest(instructions, messages, role.tools.definitions(), role.model, role.max_tokens)
        request_id = store.begin(run, "model_calls", "model_started", model=role.model, max_tokens=role.max_tokens)
        if request_id is None:
            _budget_wait(store, run, "model calls")
            return
        try:
            response = await role.complete(request, cancel)
            calls = _calls(response) if response.stop_reason is ModelStop.OK else []
        except Exception as error:  # noqa: BLE001
            response = ModelResponse(ModelStop.ERROR, "", error=str(error))
            calls = []
        if cancel.is_set() or store.task(run.task_id).state is not TaskState.RUNNING:
            return
        store.model_response(run, request_id, response.stop_reason.value, response.text, calls, response.usage, response.error)
        if run.delegation_id is None and store.latest_input(run.task_id) > request_id:
            continue
        if response.stop_reason is ModelStop.ERROR:
            store.end(run, TaskState.FAILED, RunReason.MODEL_ERROR, response.error or "Model request failed")
            return
        if response.stop_reason is ModelStop.ABORTED:
            store.end(run, TaskState.FAILED, RunReason.MODEL_ABORTED, response.error or "Model aborted the request")
            return
        if response.stop_reason is ModelStop.OK and not (response.text.strip() or calls):
            store.end(run, TaskState.FAILED, RunReason.EMPTY_RESPONSE, "Model returned an empty response")
            return
