from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from demoharness import AgentDefinition, AgentRuntime, FileCheck, TaskState, discovery
from demoharness.core.loop import ModelResponse, ModelStop
from demoharness.discovery import ObservationError, observe


def _git(repo: Path, *args: str) -> str:
    env = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _init(repo: Path) -> Path:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Observer test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "core.hooksPath", os.devnull)
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


@pytest.fixture
def repo(tmp_path):
    return _init(tmp_path / "source project")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def test_baseline_samples_committed_files_and_ignores_dirty_worktree(repo):
    (repo / "maintenance.txt").write_text("Fix the weekly report before release\n")
    _commit(repo, "Record maintenance follow-up")
    (repo / "version.txt").write_text("1.0\n")
    revision = _commit(repo, "Bump version")

    (repo / "maintenance.txt").write_text("Staged content only\n")
    _git(repo, "add", "maintenance.txt")
    (repo / "maintenance.txt").write_text("Worktree content only\n")
    (repo / "run-result.txt").write_text("Untracked runtime output\n")

    baseline = asyncio.run(observe(repo, None))
    assert baseline is not None
    assert baseline.revision == revision
    assert baseline.base_revision is None
    assert "maintenance.txt" in baseline.evidence
    assert "Fix the weekly report before release" in baseline.evidence
    assert "Staged content only" not in baseline.evidence
    assert "Worktree content only" not in baseline.evidence
    assert "run-result.txt" not in baseline.evidence
    assert asyncio.run(observe(repo, revision)) is None


def test_changed_head_reports_range_between_pinned_commits(repo):
    (repo / "report.txt").write_text("The old report\n")
    base = _commit(repo, "Initial report")
    (repo / "report.txt").write_text("The revised report\n")
    _commit(repo, "Revise report")
    (repo / "next.txt").write_text("Investigate the empty report on Mondays\n")
    revision = _commit(repo, "Record Monday issue")
    (repo / "next.txt").write_text("Local edits after commit\n")

    update = asyncio.run(observe(repo, base))
    assert update is not None
    assert update.revision == revision
    assert update.base_revision == base
    assert "report.txt" in update.evidence
    assert "next.txt" in update.evidence
    assert "-The old report" in update.evidence
    assert "+The revised report" in update.evidence
    assert "+Investigate the empty report on Mondays" in update.evidence
    assert "Local edits after commit" not in update.evidence


def test_task_worktree_commit_does_not_change_source_head(repo, tmp_path):
    (repo / "work.txt").write_text("Source state\n")
    revision = _commit(repo, "Initial snapshot")
    task = tmp_path / "task worktree"
    _git(repo, "worktree", "add", "-qb", "task-work", str(task))
    (task / "work.txt").write_text("Task output\n")
    assert _commit(task, "Task result") != revision

    assert asyncio.run(observe(repo, revision)) is None
    baseline = asyncio.run(observe(repo, None))
    assert baseline is not None
    assert baseline.revision == revision
    assert "Task output" not in baseline.evidence


def test_unchanged_head_requires_no_tree_data(repo):
    (repo / "report.txt").write_text("Tracked content\n")
    revision = _commit(repo, "Initial snapshot")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    (repo / ".git" / "objects" / tree[:2] / tree[2:]).unlink()

    assert asyncio.run(observe(repo, revision)) is None
    with pytest.raises(ObservationError):
        asyncio.run(observe(repo, None))


def test_empty_repository_and_missing_previous_commit(repo):
    assert asyncio.run(observe(repo, None)) is None
    (repo / "report.txt").write_text("Current project snapshot\n")
    revision = _commit(repo, "Initial snapshot")

    baseline = asyncio.run(observe(repo, "0" * len(revision)))
    assert baseline is not None
    assert baseline.revision == revision
    assert baseline.base_revision is None
    assert "Current project snapshot" in baseline.evidence
    with pytest.raises(ObservationError):
        asyncio.run(observe(repo, "HEAD~1"))


def test_limits_paths_and_text_separately(repo):
    for number in range(110):
        (repo / f"path-{number:03d}-{'x' * 64}.txt").write_text("short\n")
    (repo / "long.txt").write_text("Large committed text\n" + "a" * 100_000)
    revision = _commit(repo, "Large snapshot")

    baseline = asyncio.run(observe(repo, None))
    assert baseline is not None
    assert baseline.revision == revision
    assert "path-000-" in baseline.evidence
    assert "Large committed text" in baseline.evidence
    assert "path-109-" not in baseline.evidence
    assert baseline.evidence.count("[output truncated]") == 2
    assert len(baseline.evidence.encode("utf-8")) < 17_000


def test_reads_only_the_configured_repository(repo, tmp_path, monkeypatch):
    outside = _init(tmp_path / "outside project")
    (outside / "secret.txt").write_text("Data from a different Git repository\n")
    _commit(outside, "Initial outside snapshot")
    (repo / "local.txt").write_text("Authorized project text\n")
    (repo / "external-link").symlink_to(outside / "secret.txt")
    revision = _commit(repo, "Initial authorized snapshot")

    monkeypatch.setenv("GIT_DIR", str(outside / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outside))
    baseline = asyncio.run(observe(repo, None))
    assert baseline is not None
    assert baseline.revision == revision
    assert "Authorized project text" in baseline.evidence
    assert "Data from a different Git repository" not in baseline.evidence

    subdirectory = repo / "subdirectory"
    subdirectory.mkdir()
    with pytest.raises(ObservationError):
        asyncio.run(observe(subdirectory, None))


def test_git_does_not_execute_project_diff_drivers(repo, tmp_path):
    (repo / ".gitattributes").write_text("*.txt diff=project\n")
    (repo / "report.txt").write_text("Before\n")
    base = _commit(repo, "Initial snapshot")
    (repo / "report.txt").write_text("After\n")
    revision = _commit(repo, "Update report")

    marker = tmp_path / "driver-executed"
    script = repo / "diff-driver"
    script.write_text(f"#!/bin/sh\nprintf invoked > {shlex.quote(str(marker))}\n")
    script.chmod(0o755)
    _git(repo, "config", "diff.external", shlex.quote(str(script)))
    _git(repo, "config", "diff.project.textconv", shlex.quote(str(script)))

    baseline = asyncio.run(observe(repo, None))
    update = asyncio.run(observe(repo, base))
    assert baseline is not None and update is not None
    assert baseline.revision == update.revision == revision
    assert "After" in baseline.evidence
    assert "+After" in update.evidence
    assert not marker.exists()


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="FIFO is required to block Git configuration reads",
)
def test_stalled_git_read_returns_observation_error(repo, monkeypatch):
    config = repo / ".git" / "config"
    config.unlink()
    os.mkfifo(config)
    monkeypatch.setattr(discovery, "_GIT_TIMEOUT", 0.1)

    with pytest.raises(ObservationError, match="timed out"):
        asyncio.run(observe(repo, None))


def test_observation_links_a_delivered_change_and_routes_any_extra_changes(repo, tmp_path):
    (repo / "notes.txt").write_text("Initial notes\n")
    (repo / "obsolete.txt").write_text("Keep until reviewed\n")
    _commit(repo, "Initial snapshot")
    observations = asyncio.Queue()
    delivered_notes = "Updated notes\n" * 1500
    writes = [
        {
            "call_id": "new", "name": "write_file",
            "params": {"path": "artifact.txt", "content": "Delivered\n"},
        },
        {
            "call_id": "edit", "name": "write_file",
            "params": {"path": "notes.txt", "content": delivered_notes},
        },
    ]

    async def complete(request, cancel):
        if "你负责判断消息属于哪件事" in request.system_prompt:
            observations.put_nowait(json.loads(request.messages[-1]["content"])["text"])
            return ModelResponse(ModelStop.OK, '{"kind":"skip"}')
        if writes:
            pending = writes[:]
            writes.clear()
            return ModelResponse(ModelStop.OK, "", pending)
        return ModelResponse(ModelStop.OK, "Delivered")

    def apply_delivery():
        (repo / "artifact.txt").write_text("Delivered\n")
        (repo / "notes.txt").write_text(delivered_notes)

    def undo_delivery():
        (repo / "artifact.txt").unlink()
        (repo / "notes.txt").write_text("Initial notes\n")
        (repo / "obsolete.txt").write_text("Keep until reviewed\n")
        (repo / "new.txt").unlink(missing_ok=True)
        _commit(repo, "Restore base files")

    async def exercise():
        definition = AgentDefinition("demo", repo, "scripted", complete)
        state = tmp_path / "state"
        async with AgentRuntime.open(state, [definition], observe_interval=0.02) as runtime:
            assert "Initial notes" in await asyncio.wait_for(observations.get(), 5)
            agent = runtime.agent("demo")
            receipt = await agent.submit("Create artifact.txt and update notes.txt", acceptance=[
                FileCheck("artifact.txt", equals="Delivered\n"),
                FileCheck("notes.txt", equals=delivered_notes),
            ], allowed_write_paths=["artifact.txt", "notes.txt"])
            original = await agent.wait(receipt.task_id)
            assert original.state is TaskState.COMPLETED

        apply_delivery()
        _commit(repo, "Apply delivered task output")
        async with AgentRuntime.open(state, [definition], observe_interval=0.02) as runtime:
            agent = runtime.agent("demo")
            async with asyncio.timeout(5):
                async for notice in agent.updates():
                    if notice.kind == "associated":
                        assert notice.task_id == original.id
                        evidence = agent.input(notice.message_id).text
                        assert "artifact.txt" in evidence
                        assert "[output truncated]" in evidence
                        break
            assert observations.empty()
            assert agent.task(original.id).state is TaskState.COMPLETED
            assert len(agent.runs(original.id)) == 1

            undo_delivery()
            await asyncio.wait_for(observations.get(), 5)
            apply_delivery()
            (repo / "obsolete.txt").unlink()
            _commit(repo, "Apply delivery and delete an unrelated path")
            assert "obsolete.txt" in await asyncio.wait_for(observations.get(), 5)

            undo_delivery()
            await asyncio.wait_for(observations.get(), 5)
            apply_delivery()
            (repo / "new.txt").write_text("Unrelated new work\n")
            _commit(repo, "Apply delivery and add an unrelated path")
            assert "new.txt" in await asyncio.wait_for(observations.get(), 5)

            undo_delivery()
            await asyncio.wait_for(observations.get(), 5)
            apply_delivery()
            (repo / "artifact.txt").write_text("Delivered\nIndependent addition\n")
            _commit(repo, "Edit the delivered output")
            assert "Independent addition" in await asyncio.wait_for(observations.get(), 5)
            assert [task.id for task in agent.tasks()] == [original.id]

    asyncio.run(exercise())
