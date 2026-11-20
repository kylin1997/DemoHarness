"""DemoHarness 内核 · 事件流（core/events.py）。

SDK 调用方的活体通道。关键语义（见 docs/design/events-bots.md）：

- Event 形状唯一权威在此：`{task_id, session_id, type, data}`——session_id
  是通用归组键（SDK 调用方/日志/测试按会话聚合都需要）。
- 事件流只做活体推送，不是历史源（架构不变量 6）：断线/慢消费丢帧后，
  观察面靠下一个事件或主动查 storage 自愈，不补发。
- 每订阅者独立 asyncio.Queue（maxsize=256），publish 对所有订阅者
  put_nowait——队列满即丢该订阅者的一帧，publisher 永不 await、扇出不阻塞。

事件类型全集（8 类，唯一权威）见 events-bots.md：
  task_started / text_delta / tool_call_started / tool_result /
  phase_changed / task_finished / steering_received / compaction_started

三类此前「只定义常量、无 publish」的事件，其 data 形状以本模块的构造器为
唯一定义处：text_delta {text, final}、steering_received {text}、
compaction_started {summary_preview}。publish 点分别是 loop.py（text_delta）、
runtime.py 的 TaskHandle.steer（steering_received）、session.py 的 _compact
（compaction_started；压缩触发/切点/摘要生成属 compaction/ 扩展，第 6 步）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

# 事件类型全集（字符串常量，data 键名锚定在 events-bots.md 全集表）。
TASK_STARTED = "task_started"
TEXT_DELTA = "text_delta"
TOOL_CALL_STARTED = "tool_call_started"
TOOL_RESULT = "tool_result"
PHASE_CHANGED = "phase_changed"
TASK_FINISHED = "task_finished"
STEERING_RECEIVED = "steering_received"
COMPACTION_STARTED = "compaction_started"

_SUB_QUEUE_MAX = 256


class TaskPhase(str):
    """Task phase 的唯一取值集合（不另立第二套字面量）。

    loop.py 的 phase_changed 事件与 runtime.py 的 TaskStatus.phase 共用本类，
    避免「内核一套 phase 表述、门面一套 phase 表述」的不一致。
    """

    QUEUED = "queued"
    AWAITING_TOOLS = "awaiting_tools"
    TOOLS_COMPLETED = "tools_completed"
    FINAL_RESPONSE = "final_response"


@dataclass(frozen=True)
class Event:
    task_id: str
    session_id: str
    type: str
    data: dict


def text_delta_event(task_id: str, session_id: str, text: str, final: bool) -> Event:
    """TEXT_DELTA 的 publish 形状：{text, final}。

    非流式 provider 一整段文本即 final=true 终帧（events-bots.md 全集表）。
    """
    return Event(
        task_id=task_id,
        session_id=session_id,
        type=TEXT_DELTA,
        data={"text": text, "final": final},
    )


def steering_received_event(task_id: str, session_id: str, text: str) -> Event:
    """STEERING_RECEIVED 的 publish 形状：{text}（归属在 Event 的 task/session 字段）。"""
    return Event(
        task_id=task_id,
        session_id=session_id,
        type=STEERING_RECEIVED,
        data={"text": text},
    )


def compaction_started_event(task_id: str, session_id: str, summary_preview: str) -> Event:
    """COMPACTION_STARTED 的 publish 形状：{summary_preview}。

    压缩段 2 提交后由 session.py 的 _compact 发出；阈值触发与切点选择属
    compaction/ 扩展（第 6 步），本轮只冻结 publish 形状与提交点。
    """
    return Event(
        task_id=task_id,
        session_id=session_id,
        type=COMPACTION_STARTED,
        data={"summary_preview": summary_preview[:120]},
    )


class Subscription:
    """某订阅者的事件流，AsyncIterator[Event]。"""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()

    def push(self, event: Event) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # 丢帧：慢消费者不阻塞 publisher


class EventBus:
    """内核产出事件 → publish → 订阅者各自消费。"""

    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._filters: list[tuple[str | None, str | None]] = []

    def subscribe(
        self, session_id: str | None = None, task_id: str | None = None
    ) -> Subscription:
        sub = Subscription(asyncio.Queue(maxsize=_SUB_QUEUE_MAX))
        self._subs.append(sub)
        self._filters.append((session_id, task_id))
        return sub

    def publish(self, event: Event) -> None:
        for sub, (sid, tid) in zip(self._subs, self._filters):
            if sid is not None and sid != event.session_id:
                continue
            if tid is not None and tid != event.task_id:
                continue
            sub.push(event)
