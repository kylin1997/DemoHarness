"""DemoClaw 内核 · 事件流（core/events.py）。

Web 观察面的活体通道。关键语义（见 docs/design/events-bots.md）：

- Event 形状唯一权威在此：`{task_id, session_id, type, data}`——session_id
  是通用归组键（Web/日志/测试按会话聚合都需要，非 UI 专用）。
- 事件流只做活体推送，不是历史源（架构不变量 6）：断线/慢消费丢帧后，
  观察面靠下一个事件或主动查 storage 自愈，不补发。
- 每订阅者独立 asyncio.Queue（maxsize=256），publish 对所有订阅者
  put_nowait——队列满即丢该订阅者的一帧，publisher 永不 await、扇出不阻塞。

事件类型全集（8 类，唯一权威）见 events-bots.md：
  task_started / text_delta / tool_call_started / tool_result /
  phase_changed / task_finished / steering_received / compaction_started
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


@dataclass(frozen=True)
class Event:
    task_id: str
    session_id: str
    type: str
    data: dict


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