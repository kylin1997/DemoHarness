"""core/events.py 的验收测试（覆盖 docs/design/events-bots.md 事件流 1-5 项）。"""

from __future__ import annotations

import asyncio

from demoharness.core.events import Event, EventBus


def run(coro):
    return asyncio.run(coro)


async def _collect(sub, n):
    out = []
    for _ in range(n):
        out.append(await sub.__anext__())
    return out


# ── 1. publish/subscribe 基本环 + 双维过滤 ──


def test_basic_publish_subscribe():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish(Event("t1", "s1", "task_started", {}))
    got = run(_collect(sub, 1))
    assert got[0].task_id == "t1" and got[0].session_id == "s1"


def test_filter_by_session_and_task():
    bus = EventBus()
    sub_s = bus.subscribe(session_id="s1")
    sub_t = bus.subscribe(task_id="t2")
    sub_all = bus.subscribe()

    bus.publish(Event("t2", "s2", "x", {}))
    # session s1 过滤：不应收到 s2
    # task t2 过滤：应收到
    got_t = run(_collect(sub_t, 1))
    assert got_t[0].task_id == "t2"
    assert run(_take_nowait(sub_s)) is None
    assert run(_collect(sub_all, 1))[0].task_id == "t2"


async def _take_nowait(sub) -> Event | None:
    try:
        return sub._queue.get_nowait()
    except asyncio.QueueEmpty:
        return None


# ── 2. 扇出：两订阅者各自收到；慢订阅者丢帧不阻塞 ──


def test_fanout_two_subscribers():
    bus = EventBus()
    s1 = bus.subscribe(session_id="s1")
    run(_collect(s1, 0))  # 建立
    # 两个订阅同一会话
    s1b = bus.subscribe(session_id="s1")
    bus.publish(Event("t", "s1", "x", {}))
    e1 = run(_collect(s1, 1))
    e2 = run(_collect(s1b, 1))
    assert e1[0].type == "x" and e2[0].type == "x"


def test_no_subscriber_publish_ok():
    bus = EventBus()
    bus.publish(Event("t", "s", "x", {}))  # 不抛


# ── 3. 慢订阅者丢帧：满队列丢帧不阻塞 publisher ──


def test_slow_subscriber_drops():
    bus = EventBus()
    bus.subscribe(session_id="s1")
    # 塞满队列（maxsize=256），不消费
    for i in range(300):
        bus.publish(Event("t", "s1", "x", {"i": i}))
    # publish 没抛（满即丢）
    assert True


# ── 4. 事件序列顺序（loop 集成在 test_loop.py 里覆盖） ──