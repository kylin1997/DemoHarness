"""runtime.py 门面（第 0 步）的集成冒烟测试。

验证最小 SDK 闭环：build(mock) → bot.run() → TaskResult。用 mock stream_fn
保持确定性，不依赖网络。
"""

from __future__ import annotations

from democlaw.core.loop import ModelResponse, ModelStop
from democlaw.core.task import StopReason
from democlaw.runtime import BotRuntime, RuntimeConfig


def _mk_bots_dir(tmp_path):
    bots_dir = tmp_path / "bots"
    (bots_dir / "architect").mkdir(parents=True)
    (bots_dir / "architect" / "SOUL.md").write_text("你是架构师", encoding="utf-8")
    (bots_dir / "architect" / "AGENTS.md").write_text("完成任务", encoding="utf-8")
    (bots_dir / "architect" / "spec.json").write_text('{"model": null, "tools": null}', encoding="utf-8")
    return bots_dir


def test_build_run_closes_loop(tmp_path):
    async def mock_stream(request, cancel):
        return ModelResponse(ModelStop.OK, "你好，已完成", [])

    bots_dir = _mk_bots_dir(tmp_path)
    rt = BotRuntime.build(
        RuntimeConfig(db_path=str(tmp_path / "rt.db"), stream_fn=mock_stream, bots_dir=bots_dir)
    )
    result = rt.bot("architect").run("写一份设计")
    assert result.stop_reason == StopReason.COMPLETED
    assert result.content == "你好，已完成"
    # 会话已建
    assert len(rt.sessions()) == 1
    rt.close()


def test_run_requires_api_key_without_mock(tmp_path):
    bots_dir = _mk_bots_dir(tmp_path)
    rt = BotRuntime.build(RuntimeConfig(db_path=str(tmp_path / "no.db"), bots_dir=bots_dir))
    try:
        rt.bot("architect").run("hi")
        assert False, "should have raised"
    except RuntimeError as e:
        assert "API key" in str(e)
    rt.close()


def test_session_tree_verbs(tmp_path):
    bots_dir = _mk_bots_dir(tmp_path)
    rt = BotRuntime.build(RuntimeConfig(db_path=str(tmp_path / "tree.db"), bots_dir=bots_dir))
    s = rt.new_session("architect")
    assert s.id.startswith("s_")
    assert len(rt.sessions()) == 1
    # history 空
    assert rt.history(s.id) == []
    rt.delete_session(s.id)
    assert rt.sessions() == []
    rt.close()


def test_sessions_with_interrupted(tmp_path):
    """模拟：一个会话被 interrupted，sessions_with(INTERRUPTED) 能发现它。"""
    from democlaw.core.session import AppendOp, NewOp
    from democlaw.core.task import materialize_interrupted

    rt = BotRuntime.build(RuntimeConfig(db_path=str(tmp_path / "rec.db")))
    # 直接操作 core 层构造 interrupted 会话（门面层无运行中的任务）
    import asyncio

    async def build_interrupted():
        info = await rt._session_mgr.submit("", NewOp("", bot_id="architect"))
        await rt._session_mgr.submit(info.id, AppendOp(info.id, "user", "做点事", parent=None, task_id="t1"))
        await materialize_interrupted(rt._session_mgr, info.id)
        return info.id

    sid = asyncio.run(build_interrupted())
    found = rt.sessions_with(StopReason.INTERRUPTED)
    assert any(s.id == sid for s in found)
    rt.close()


def test_start_async_handle(tmp_path):
    """start() 提交立即返回 TaskHandle；aresult() 等到终态；events() 推事件。"""
    import asyncio

    async def mock_stream(request, cancel):
        return ModelResponse(ModelStop.OK, "完成", [])

    bots_dir = _mk_bots_dir(tmp_path)
    rt = BotRuntime.build(RuntimeConfig(db_path=str(tmp_path / "start.db"), stream_fn=mock_stream, bots_dir=bots_dir))

    async def go():
        bot = rt.bot("architect")
        handle = bot.start("做任务")
        assert handle.status().phase == "queued"
        events = []
        async for ev in handle.events():
            events.append(ev.type)
            if ev.type == "task_finished":
                break
        result = await handle.aresult()
        return result, events

    result, events = asyncio.run(go())
    assert result.stop_reason == StopReason.COMPLETED
    assert result.content == "完成"
    assert "task_started" in events and "task_finished" in events
    # session_tasks 能看到这条任务
    statuses = rt.session_tasks(result.session_id)
    assert len(statuses) == 1
    rt.close()


def test_start_steer_and_cancel(tmp_path):
    """steer 注入 + cancel 取消。"""
    import asyncio

    seen = []

    async def mock_stream(request, cancel):
        seen.append(request)
        if cancel.is_set():
            return ModelResponse(ModelStop.ABORTED, "", [])
        # 贴近真实：工具调用已落盘、结果未落盘之前持续返回工具调用
        return ModelResponse(ModelStop.OK, "", [{"call_id": "c1", "name": "read_file", "params": {"path": "x"}}])

    bots_dir = _mk_bots_dir(tmp_path)
    rt = BotRuntime.build(RuntimeConfig(db_path=str(tmp_path / "cancel.db"), stream_fn=mock_stream, bots_dir=bots_dir))

    async def go():
        bot = rt.bot("architect")
        handle = bot.start("做任务")
        # 运行中 steer（会话已同步建好，句柄从一开始就持真实 session_id）
        await handle.steer("顺便查一下 Y")
        # 取消
        handle.cancel()
        result = await handle.aresult()
        return result

    result = asyncio.run(go())
    assert result.stop_reason == StopReason.CANCELLED
    rt.close()
