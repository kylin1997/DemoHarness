"""验证会话持久化、日志恢复、命令审批和技能匹配的正常路径及失败边界。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tinyharness.guardrails import (
    INPUT_LIMIT,
    OUTPUT_MARKER,
    Guardrails,
    validate_command_argv,
)
from tinyharness.skills import Skills
from tinyharness.state import Store


def test_store_survives_reopen(tmp_path):
    path = tmp_path / "data" / "sessions"
    store = Store(path)
    first = store.create_session()
    second = store.create_session()
    session_id = first["id"]
    store.rename(session_id, "笔记核对")
    store.update_options(session_id, {"reasoning": "high"})
    store.update_options(session_id, {"mode": "plan"})
    message = {"role": "user", "content": "核对笔记"}
    event = {"type": "stage", "stage": "input"}
    store.add_message(session_id, message)
    store.add_message(session_id, {"role": "assistant", "content": "已完成"})
    store.save_window(session_id, [message])
    store.append_event(session_id, event)
    store.remember(session_id, "Evidence is in notes.md")
    store.trust(session_id, '["printf","hello"]')
    expected_session = store.session(session_id)
    store.close()

    session_path = path / session_id
    snapshot = json.loads((session_path / "state.json").read_text(encoding="utf-8"))
    assert snapshot == expected_session | {
        "context_window": [message],
        "trusted_commands": ['["printf","hello"]'],
    }
    history = (session_path / "history.jsonl").read_text(encoding="utf-8")
    assert history.endswith("\n")
    rows = [json.loads(line) for line in history.splitlines()]
    assert all(set(row) == {"kind", "at", "value"} for row in rows)
    assert [(row["kind"], row["value"]) for row in rows] == [
        ("message", message),
        ("message", {"role": "assistant", "content": "已完成"}),
        ("event", event),
        ("memory", "Evidence is in notes.md"),
    ]

    store = Store(path)
    try:
        session = store.session(session_id)
        assert session["title"] == "笔记核对"
        assert session["options"] == {
            "profile": "demo",
            "reasoning": "high",
            "mode": "plan",
        }
        assert store.list_sessions() == [session, second]
        assert store.messages(session_id) == [
            message,
            {"role": "assistant", "content": "已完成"},
        ]
        assert store.window(session_id) == [message]
        assert store.events(session_id) == [event]
        assert store.recall(session_id) == ["Evidence is in notes.md"]
        assert store.is_trusted(session_id, '["printf","hello"]')
        assert not store.is_trusted(second["id"], '["printf","hello"]')
    finally:
        store.close()


def test_history_append_updates_recency_without_replacing_snapshot(
    tmp_path, monkeypatch
):
    path = tmp_path / "sessions"
    store = Store(path)
    now = 1.0
    monkeypatch.setattr("tinyharness.state.time.time", lambda: now)
    first = store.create_session()["id"]
    now = 2.0
    second = store.create_session()
    snapshot = path / first / "state.json"
    initial = snapshot.read_bytes()
    now = 3.0
    store.append_event(first, {"type": "done"})
    assert snapshot.read_bytes() == initial
    updated = store.session(first)
    assert updated["updated_at"] == now
    assert store.list_sessions() == [updated, second]
    store.close()

    reopened = Store(path)
    try:
        assert reopened.list_sessions() == [updated, second]
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "partial",
    [
        b'{"kind":"event","value":',
        b'{"kind":"event","at":42,"value":{"type":"partial"}}',
    ],
)
def test_store_recovers_interrupted_history_tail(tmp_path, partial):
    path = tmp_path / "sessions"
    store = Store(path)
    session_id = store.create_session()["id"]
    event = {"type": "start"}
    store.append_event(session_id, event)
    store.close()

    history = path / session_id / "history.jsonl"
    complete = history.read_bytes()
    history.write_bytes(complete + partial)

    store = Store(path)
    try:
        assert history.read_bytes() == complete
        assert store.events(session_id) == [event]
        store.append_event(session_id, {"type": "done"})
        assert history.read_bytes().startswith(complete)
        assert store.events(session_id) == [event, {"type": "done"}]
    finally:
        store.close()


@pytest.mark.parametrize(
    "bad_row", [b"{bad}\n", b'{"kind":"event","at":1,"value":[]}\n']
)
def test_store_rejects_malformed_complete_rows_and_releases_lock(tmp_path, bad_row):
    path = tmp_path / "sessions"
    store = Store(path)
    session_id = store.create_session()["id"]
    store.close()

    history = path / session_id / "history.jsonl"
    history.write_bytes(bad_row)
    with pytest.raises(ValueError, match="Invalid history record"):
        Store(path)
    assert history.read_bytes() == bad_row

    history.write_bytes(b"")
    reopened = Store(path)
    try:
        assert reopened.events(session_id) == []
    finally:
        reopened.close()


def test_store_holds_writer_lock_until_close(tmp_path):
    path = tmp_path / "sessions"
    store = Store(path)
    try:
        with pytest.raises(RuntimeError, match="Session store is already open"):
            Store(path)
        session = store.create_session()
    finally:
        store.close()

    reopened = Store(path)
    try:
        assert reopened.list_sessions() == [session]
        with pytest.raises(RuntimeError, match="Session store is closed"):
            store.create_session()
    finally:
        reopened.close()


def test_store_discards_malformed_trust_list(tmp_path):
    path = tmp_path / "sessions"
    store = Store(path)
    session_id = store.create_session()["id"]
    signature = '["printf","hello"]'
    store.trust(session_id, signature)
    store.close()

    snapshot = path / session_id / "state.json"
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    data["trusted_commands"].append(None)
    snapshot.write_text(json.dumps(data), encoding="utf-8")

    store = Store(path)
    try:
        assert not store.is_trusted(session_id, signature)
        store.trust(session_id, signature)
        assert store.is_trusted(session_id, signature)
    finally:
        store.close()


def test_failed_snapshot_replace_leaves_previous_session_state(tmp_path, monkeypatch):
    path = tmp_path / "sessions"
    store = Store(path)
    try:
        session = store.create_session()
        session_path = path / session["id"]
        initial = (session_path / "state.json").read_bytes()

        def fail_replace(self, target):
            raise OSError("Snapshot replace failed")

        monkeypatch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError, match="Snapshot replace failed"):
            store.rename(session["id"], "Uncommitted title")
        assert store.session(session["id"]) == session
        assert (session_path / "state.json").read_bytes() == initial
        assert {file.name for file in session_path.iterdir()} == {
            "state.json",
            "history.jsonl",
        }
    finally:
        store.close()


def test_approval_respects_exact_argv_and_session_after_reopen(tmp_path):
    path = tmp_path / "sessions"
    store = Store(path)
    first = store.create_session()["id"]
    second = store.create_session()["id"]

    async def request(guard, session_id, argv, decision=None):
        events = []

        async def emit(kind, **payload):
            events.append((kind, payload))
            if kind == "approval":
                assert decision is not None
                approval_id = payload["approval_id"]
                other_session = first if session_id == second else second
                assert not guard.resolve(other_session, approval_id, decision)
                assert guard.resolve(session_id, approval_id, decision)
                assert not guard.resolve(session_id, approval_id, decision)

        reason = await guard.invoke_with_approval(
            session_id, "run_command", {"argv": argv}, emit
        )
        if decision:
            assert [kind for kind, _ in events] == ["approval", "approval_resolved"]
            assert events[1][1]["decision"] == decision
        else:
            assert not events
        return reason

    async def initial():
        guard = Guardrails(store)
        assert await request(guard, first, ["ls", "-la"]) is None
        assert await request(guard, first, ["printf", "hello"], "allow") is None
        assert await request(guard, first, ["printf", "hello"], "trust") is None

    asyncio.run(initial())
    store.close()
    store = Store(path)

    async def reopened():
        guard = Guardrails(store)
        assert await request(guard, first, ["printf", "hello"]) is None
        assert await request(guard, first, ["printf", "hello", ""], "deny")
        assert await request(guard, second, ["printf", "hello"], "deny")
        assert await request(guard, first, ["ls", "-l"], "deny")

    try:
        asyncio.run(reopened())
    finally:
        store.close()


def test_approval_cleans_up_after_timeout_and_cancellation(tmp_path):
    store = Store(tmp_path / "sessions")
    session_id = store.create_session()["id"]

    async def exercise():
        guard = Guardrails(store, approval_timeout=0)
        events = []

        async def emit(kind, **payload):
            events.append((kind, payload))

        args = {"argv": ["printf", "hello"]}
        assert await guard.invoke_with_approval(session_id, "run_command", args, emit)
        assert events[-1][1]["decision"] == "timeout"
        assert not guard.resolve(session_id, events[0][1]["approval_id"], "allow")

        guard = Guardrails(store)
        opened = asyncio.Future()

        async def on_open(kind, **payload):
            if kind == "approval":
                opened.set_result(payload["approval_id"])

        task = asyncio.create_task(
            guard.invoke_with_approval(session_id, "run_command", args, on_open)
        )
        approval_id = await opened
        task.cancel()
        (result,) = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result, asyncio.CancelledError)
        assert not guard.resolve(session_id, approval_id, "trust")

    try:
        asyncio.run(exercise())
    finally:
        store.close()


def test_guard_markers_and_command_validation(tmp_path):
    store = Store(tmp_path / "sessions")
    guard = Guardrails(store)
    try:
        assert guard.check_input("hello") is None
        assert guard.check_input("a" * INPUT_LIMIT) is None
        assert guard.check_input("a" * (INPUT_LIMIT + 1))
        assert guard.check_input("　")
        assert guard.check_input("x tinyharness_block_input y")
        assert guard.check_tool("write", {"content": ["tinyharness_block_tool"]})
        assert guard.check_output(f"start {OUTPUT_MARKER.lower()} end")
        assert guard.output_holdback == len(OUTPUT_MARKER) - 1
        assert validate_command_argv(["printf", "hello", ""]) is None
        assert validate_command_argv(["./local.exe"])
        assert validate_command_argv(["printf", "bad\x00arg"])
        assert validate_command_argv(["printf", 5])
    finally:
        store.close()


def test_skills_disclose_only_headers_until_trigger(tmp_path):
    sample_path = Path(__file__).resolve().parents[1] / "skills"
    demo = Skills(sample_path)
    assert "工作区笔记核对" in demo.index()
    assert "先读取" not in demo.index()
    assert not demo.match_input("你好")
    name, body = demo.match_input("请核对 workspace 里的 notes.md")[0]
    assert name == "工作区笔记核对"
    assert "先读取 `notes.md`" in body

    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text(
        '---\nname: check\ndescription: Quick check\ntriggers: [review, "ship, now"]\n---\nfull body',
        encoding="utf-8",
    )
    skills = Skills(tmp_path)
    assert "full body" not in skills.index()
    assert skills.match_input("Ship, NOW") == [("check", "full body")]
    assert not skills.match_input("ship")
    skill_path.write_text(
        "# Plain Skill\n\nSmall header\n\nFull instructions", encoding="utf-8"
    )
    skills = Skills(skill_path)
    assert "Small header" in skills.index()
    assert "Full instructions" in skills.match_input("Plain skill")[0][1]
