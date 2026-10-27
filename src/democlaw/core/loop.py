"""DemoClaw 内核 · 任务循环骨架（core/loop.py）。

**唯一的真循环**（task.py 是纯函数库，见 loop-providers.md「模块收敛」）。
ReAct 形态：思考 → 工具 → 观察 → 再判断，直到终态。

注入点契约（架构「内核与扩展的边界」）：
- stream_fn：调用模型的函数，每轮调用一次，**不得抛异常**（一切失败编码进
  ModelResponse.stop_reason）。providers/ 是具体实现。
- finish_turn：每轮结束时的决策函数，返回 {end, continue}；结果验证经此
  消费证据。
- before/after 工具钩子经 tools.py 三段式，loop 不直接管权限。

三相 ↔ loop 步骤 ↔ append 落点（唯一权威 = loop-providers.md 对应表）：
  input → 进循环前 append user entry（task_id）→ mark_running
  awaiting_tools → 模型返回工具调用 → append tool_call entries
  tools_completed → 三段式执行完 → append tool_result entries
  final_response → 终答 → append 终态 assistant entry（content 带 stop_reason）

stop_reason 六态触发源：completed=模型无工具调用 / error=stream_fn 编码错误
（含 LENGTH 过渡期）/ max_iterations=预算 / cancelled=信号 / empty_response=
模型空返回 / interrupted=恢复物化专属（本模块异常路径调 task.py 物化）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .events import (
    PHASE_CHANGED,
    TASK_FINISHED,
    TASK_STARTED,
    Event,
    EventBus,
)
from .session import AppendOp, SessionManager
from .storage import EntryRow
from .task import (
    StopReason,
    materialize_interrupted,
    terminal_content,
)
from .tools import ToolRegistry, ToolSpec, execute_tool_calls


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
FinishTurn = Callable[["LoopContext"], bool]


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


@dataclass
class LoopRuntime:
    """loop 需要的全部注入依赖（BotRuntime 装配期拼装）。"""

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
        else:
            # reset / tool_call：跳过
            i += 1
    return messages


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
            # a. steering drain：执行本会话 FIFO 里排队的 steering（user）append
            await _drain_steering(runtime, task_id, sid)

            # b. 组装上下文（从活跃叶重放 → OpenAI 投影）
            prefix = await _replay_prefix(runtime.session_mgr, sid)
            messages = project_context(prefix)
            request = ModelRequest(
                system_prompt=runtime.system_prompt,
                messages=messages,
                tools=runtime.registry.definitions(),
                model=runtime.model,
                max_tokens=None,
            )

            # c. 调用模型（stream_fn 不得抛异常）
            resp = await runtime.stream_fn(request, cancel)

            # d. 响应分类 → 映射到任务六态
            if resp.stop_reason == ModelStop.ABORTED:
                return await _finish(runtime, sid, task_id, "", StopReason.CANCELLED, iterations, tools_used)
            if resp.stop_reason == ModelStop.ERROR:
                return await _finish(
                    runtime, sid, task_id, "", StopReason.ERROR, iterations, tools_used,
                    error=resp.error or "模型调用错误",
                )
            if resp.stop_reason == ModelStop.LENGTH:
                return await _finish(
                    runtime, sid, task_id, "", StopReason.ERROR, iterations, tools_used,
                    error="上下文超限（LENGTH）",
                )
            if not resp.text and not resp.tool_calls:
                return await _finish(runtime, sid, task_id, "", StopReason.EMPTY_RESPONSE, iterations, tools_used)

            if resp.tool_calls:
                await _append(runtime, sid, "assistant", _assistant_content(resp.text, resp.tool_calls))
                runtime.event_bus.publish(
                    Event(task_id=task_id, session_id=sid, type=PHASE_CHANGED, data={"phase": "awaiting_tools"})
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
                results = await execute_tool_calls(calls, runtime.registry, cancel=cancel)
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
                    Event(task_id=task_id, session_id=sid, type=PHASE_CHANGED, data={"phase": "tools_completed"})
                )
                iterations += 1
                if iterations >= runtime.max_iterations:
                    return await _budget_finish(runtime, sid, task_id, iterations, tools_used, cancel)
                continue

            # 纯文本（无工具调用）→ 终态 completed
            return await _finish(runtime, sid, task_id, resp.text, StopReason.COMPLETED, iterations, tools_used)
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


async def _budget_finish(
    runtime: LoopRuntime, sid: str, task_id: str, iterations: int, tools_used: int, cancel: asyncio.Event
) -> TaskResult:
    """预算耗尽：发一次「直接作答勿用工具」，模型仍返回工具调用则丢弃取文本；
    文本空 → max_iterations。"""
    prefix = await _replay_prefix(runtime.session_mgr, sid)
    request = ModelRequest(
        system_prompt=runtime.system_prompt,
        messages=project_context(prefix),
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
    return {
        "id": tool_call.get("call_id"),
        "type": "function",
        "function": {
            "name": tool_call.get("name"),
            "arguments": json.dumps(tool_call.get("params", {})),
        },
    }


async def _append(runtime: LoopRuntime, sid: str, kind: str, content: str) -> str:
    return await runtime.session_mgr.submit(sid, AppendOp(sid, kind, content, parent=None))


async def _replay_prefix(session_mgr: SessionManager, sid: str) -> list[EntryRow]:
    from .session import HistoryOp

    return await session_mgr.submit(sid, HistoryOp(sid))


async def _drain_steering(runtime: LoopRuntime, task_id: str, sid: str) -> None:
    """执行本会话 FIFO 里排队的 steering append。

    第 4 步占位：steering 的完整语义（运行中追加 user entry → 下一轮上下文
    自然携带）依赖门面层（第 0 步）把 steer 消息翻译成 AppendOp 并投入 FIFO；
    FIFO 串行化保证它们会在本循环的 append 之间被按序消费。这里无需额外地
    drain——steering append 一经提交即进入会话历史，重放自然携带。
    """
    return


def _loads(s: str) -> dict:
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return {} if not s else {"_raw": s}