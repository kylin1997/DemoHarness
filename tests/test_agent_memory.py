import sqlite3
from contextlib import closing

import pytest

from demoharness.memory import MemoryConflict, MemoryDraft, MemoryStore

DAY = 86400


@pytest.fixture
def db():
    with closing(sqlite3.connect(":memory:")) as conn:
        yield conn


def test_recall_is_scoped_ranked_and_bounded(db):
    store = MemoryStore(db, clock=lambda: 100.0)
    global_preference, = store.record("helper", None, "input:1", [
        MemoryDraft(
            "preference", "pytest", "Run pytest before delivery.", ("input:1",), "explicit",
        ),
    ])
    project_experience, = store.record("helper", "app", "task:app:done", [
        MemoryDraft(
            "experience", "checkout regression", "购物车的回归测试用pytest运行。",
            ("task:app", "entry:verify"), "verified",
        ),
    ])
    store.record("helper", "service", "task:app:done", [
        MemoryDraft("experience", "pytest", "购物车 uses pytest.", ("task:app",), "verified"),
    ])
    store.record("reviewer", "app", "task:app:done", [
        MemoryDraft("experience", "pytest", "购物车 uses pytest.", ("task:app",), "verified"),
    ])

    assert store.recall("helper", "app", "购物车 pytest", limit=1) == [project_experience]
    assert store.recall("helper", "app", "购物车 pytest") == [
        project_experience, global_preference,
    ]
    assert store.recall("helper", None, "pytest") == [global_preference]
    assert store.recall("helper", "app", "Please plan the new logo") == []
    assert store.recall("helper", "app", "pytest", limit=0) == []
    assert store.list("helper", "app") == [project_experience]
    assert project_experience.sources == ("task:app", "entry:verify")
    assert project_experience.updated_at == 100.0


def test_inference_and_conflict_require_a_user_revision_or_evidence(db):
    store = MemoryStore(db)
    inferred, = store.record("helper", "app", "task:one", [
        MemoryDraft("fact", "test runner", "The test runner is pytest.", ("entry:1",)),
    ])
    assert inferred.state == "candidate"
    assert store.recall("helper", "app", "test runner") == []

    original, = store.record("helper", "app", "task:two", [
        MemoryDraft(
            "fact", "Test Runner", "The test runner is pytest.", ("entry:2",), "verified",
        ),
    ])
    assert store.get("helper", inferred.id).state == "archived"
    conflict, = store.record("helper", "app", "task:three", [
        MemoryDraft(
            "fact", "test runner", "The test runner is unittest.", ("entry:3",), "verified",
        ),
    ])
    assert store.list("helper", state="candidate") == [conflict]
    assert store.recall("helper", "app", "test runner") == [original]

    with pytest.raises(MemoryConflict):
        store.correct("helper", original.id, original.version + 1, "Wrong version.", ("input:2",))
    with pytest.raises(MemoryConflict):
        store.correct("reviewer", original.id, original.version, "Wrong Agent.", ("input:2",))
    revised = store.correct(
        "helper", original.id, original.version, "The test runner is unittest.", ("input:3",),
    )
    assert revised.supersedes == original.id
    assert revised.version == original.version + 1
    assert revised.sources == ("input:3",)
    assert store.get("helper", original.id).text == "The test runner is pytest."
    assert store.get("helper", original.id).state == "archived"
    assert store.get("helper", conflict.id).state == "archived"
    assert store.recall("helper", "app", "test runner") == [revised]
    with pytest.raises(MemoryConflict):
        store.correct("helper", original.id, original.version, "Stale revision.", ("input:4",))

    late, = store.record("helper", "app", "task:four", [
        MemoryDraft("fact", "test runner", original.text, ("entry:4",), "verified"),
    ])
    assert late.state == "candidate"
    assert store.recall("helper", "app", "test runner") == [revised]


def test_expiry_is_immediate_and_archives_are_kept_until_retention(db):
    now = 100.0
    store = MemoryStore(db, clock=lambda: now)
    temporal = MemoryDraft("fact", "release", "Release freeze this week.",
                           ("input:1",), "explicit", valid_until=200)
    record, stable = store.record("helper", "app", "input:1", [
        temporal,
        MemoryDraft("preference", "release notes", "Keep release notes short.",
                    ("input:1",), "explicit"),
    ])
    assert set(store.recall("helper", "app", "release")) == {record, stable}

    now = 200
    assert store.recall("helper", "app", "release") == [stable]
    assert store.get("helper", record.id).state == "archived"
    assert store.maintain("helper") == 0
    now += 30 * DAY - 1
    assert store.maintain("helper") == 0
    now += 1
    assert store.maintain("helper") == 1
    assert store.get("helper", record.id) is None
    assert store.list("helper") == [stable]
    assert store.record("helper", "app", "another-checkpoint", [temporal]) == []


def test_delete_and_source_replay_are_durable(tmp_path):
    path = tmp_path / "agent.sqlite"
    note = MemoryDraft("experience", "checkout", "Run checkout tests.", ("entry:1",), "verified")
    duplicate = MemoryDraft(
        "experience", "checkout", note.text, ("entry:2",), "verified",
    )
    with closing(sqlite3.connect(path)) as conn:
        store = MemoryStore(conn)
        original, = store.record("helper", "app", "done:1", [note])
        assert store.record("helper", "app", "done:1", [note]) == []
        assert store.record("helper", "app", "done:2", [duplicate]) == []
        assert not store.delete("reviewer", original.id)
        assert store.recall("helper", "app", "checkout") == [original]
        assert store.delete("helper", original.id)
        assert not store.delete("helper", original.id)
        assert note.text not in "\n".join(conn.iterdump())

    with closing(sqlite3.connect(path)) as conn:
        store = MemoryStore(conn)
        assert store.processed("helper", "app", "done:1")
        assert store.get("helper", original.id) is None
        assert store.record("helper", "app", "done:1", [note]) == []
        assert store.record("helper", "app", "done:3", [note, duplicate]) == []
        assert store.recall("helper", "app", "checkout") == []
        new, = store.record("helper", "app", "input:1", [
            MemoryDraft("experience", "checkout", note.text, ("input:1",), "explicit"),
        ])
        assert new.id != original.id
        assert store.recall("helper", "app", "checkout") == [new]


def test_checkpoint_and_records_rollback_with_the_host(tmp_path):
    path = tmp_path / "agent.sqlite"
    note = MemoryDraft("preference", "vim", "Use Vim.", ("input:1",), "explicit")
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        store = MemoryStore(conn)
        conn.execute("CREATE TABLE host_event (id INTEGER)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO host_event VALUES (1)")
        saved, = store.record("helper", None, "input:1", [note])
        assert store.processed("helper", None, "input:1")
        assert store.get("helper", saved.id) == saved
        conn.rollback()
        assert not store.processed("helper", None, "input:1")
        assert store.list("helper") == []
        assert conn.execute("SELECT COUNT(*) FROM host_event").fetchone()[0] == 0
        saved, = store.record("helper", None, "input:1", [note])

    with closing(sqlite3.connect(path)) as conn:
        store = MemoryStore(conn)
        assert store.processed("helper", None, "input:1")
        assert store.recall("helper", None, "vim") == [saved]


def test_bad_draft_rolls_back_the_batch_without_rolling_back_the_host(db):
    store = MemoryStore(db)
    first = MemoryDraft("preference", "vim", "Use Vim.", ("input:1",), "explicit")
    invalid = MemoryDraft("fact", "release", "Release on Mondays.", ())
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError):
        store.record("helper", None, "input:1", [first, invalid])
    assert db.in_transaction
    assert store.list("helper") == []
    assert not store.processed("helper", None, "input:1")
    store.record("helper", None, "input:1", [first])
    assert store.processed("helper", None, "input:1")
    db.commit()


@pytest.mark.parametrize("text", [
    "Password = !example-only",
    '{"openai_api_key":"example-only"}',
    "管理员密码是「example-only」",
    "Authorization: Bearer example-only",
    "https://user:example-only@example.com",
    "-----BEGIN PRIVATE KEY-----\nexample-only",
])
def test_credential_text_is_never_stored(db, text):
    store = MemoryStore(db)
    assert store.record("helper", None, "input:1", [
        MemoryDraft("preference", "configuration", text, ("input:1",), "explicit"),
    ]) == []
    assert store.processed("helper", None, "input:1")
    assert store.list("helper") == []
    assert "example-only" not in "\n".join(db.iterdump())


def test_credentials_in_metadata_and_corrections_are_filtered(db):
    store = MemoryStore(db)
    clean, = store.record("helper", None, "input:1", [
        MemoryDraft("preference", "login", "Use a password manager.", ("input:1",), "explicit"),
        MemoryDraft("fact", "password=example-only", "Old login.", ("entry:1",), "verified"),
        MemoryDraft("fact", "credentials", "Old login.", ("token=example-only",), "verified"),
    ])
    with pytest.raises(ValueError):
        store.correct("helper", clean.id, clean.version, "Password: example-only", ("input:2",))
    assert store.list("helper") == [clean]
    assert store.recall("helper", None, "password manager") == [clean]
    assert "example-only" not in "\n".join(db.iterdump())
