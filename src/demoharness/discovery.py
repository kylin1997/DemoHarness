"""Read bounded evidence from a configured project's committed Git history."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT = 5.0
_PATH_BYTES = 4_096
_TEXT_BYTES = 12_288
_OBJECT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


@dataclass(frozen=True)
class Observation:
    revision: str
    base_revision: str | None
    evidence: str


class ObservationError(RuntimeError):
    """The configured project's committed snapshot could not be read."""


async def observe(repo: Path, previous_revision: str | None) -> Observation | None:
    """Read the current HEAD once. Use a baseline sample if the old commit is absent."""
    if previous_revision is not None and not _OBJECT_ID.fullmatch(previous_revision):
        raise ObservationError("The previous revision must be a full Git object ID")

    repo = repo.resolve()
    head = await _git(
        repo,
        "rev-parse",
        "--show-prefix",
        "--verify",
        "--quiet",
        "--end-of-options",
        "HEAD^{commit}",
        max_bytes=256,
        allow_empty=True,
    )
    if not head:
        return None
    prefix, _, revision = head.partition("\n")
    revision = revision.strip()
    if prefix or not _OBJECT_ID.fullmatch(revision):
        raise ObservationError(
            "The configured project must be the root of a Git repository"
        )
    if revision == previous_revision:
        return None

    if previous_revision is not None:
        old = await _git(
            repo,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{previous_revision}^{{commit}}",
            max_bytes=128,
            allow_empty=True,
        )
        if old.strip() == previous_revision:
            args = (
                "diff-tree",
                "--no-commit-id",
                "-r",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--no-relative",
                "--no-color",
                "--ignore-submodules=all",
                f"-O{os.devnull}",
            )
            paths = await _git(
                repo,
                *args,
                "--name-status",
                previous_revision,
                revision,
                "--",
                max_bytes=_PATH_BYTES,
                attributes=revision,
            )
            patch = await _git(
                repo,
                *args,
                "--patch",
                "--unified=2",
                "--full-index",
                previous_revision,
                revision,
                "--",
                max_bytes=_TEXT_BYTES,
                attributes=revision,
            )
            evidence = f"Changed paths\n{paths}\nCommitted diff\n{patch}"
            return Observation(revision, previous_revision, evidence)

    paths = await _git(
        repo,
        "ls-tree",
        "--full-tree",
        "-r",
        "--name-only",
        revision,
        "--",
        max_bytes=_PATH_BYTES,
    )
    sample = await _git(
        repo,
        "grep",
        "--no-textconv",
        "--no-recurse-submodules",
        "--no-color",
        "--full-name",
        "--heading",
        "--line-number",
        "--threads=1",
        "-I",
        "-m",
        "6",
        "-G",
        "-e",
        ".",
        revision,
        "--",
        max_bytes=_TEXT_BYTES,
        allow_empty=True,
        attributes=revision,
    )
    evidence = f"Committed paths\n{paths}\nSampled committed text\n{sample}"
    return Observation(revision, None, evidence)


async def _git(
    repo: Path,
    command: str,
    *args: str,
    max_bytes: int,
    allow_empty: bool = False,
    attributes: str | None = None,
) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ObservationError("Git is required to observe project commits")
    git = Path(executable).resolve()
    if git.is_relative_to(repo):
        raise ObservationError("Git must be installed outside the observed project")

    env = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ATTR_NOSYSTEM="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_ALLOW_PROTOCOL="",
        GIT_CEILING_DIRECTORIES=str(repo.parent),
        LC_ALL="C",
    )
    if attributes is not None:
        env["GIT_ATTR_SOURCE"] = attributes

    try:
        async with asyncio.timeout(_GIT_TIMEOUT):
            process = await asyncio.create_subprocess_exec(
                str(git),
                "-C",
                str(repo),
                "--no-pager",
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.attributesFile={os.devnull}",
                "-c",
                "core.quotePath=false",
                command,
                *args,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=max_bytes + 1,
            )
            try:
                assert process.stdout is not None
                output = await process.stdout.readexactly(max_bytes + 1)
                return (
                    output[:max_bytes].decode("utf-8", "replace")
                    + "\n[output truncated]\n"
                )
            except asyncio.IncompleteReadError as end:
                status = await process.wait()
                if status == 1 and allow_empty:
                    return ""
                if status != 0:
                    raise ObservationError(f"git {command} exited with status {status}")
                return end.partial.decode("utf-8", "replace")
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.communicate()
    except TimeoutError as exc:
        raise ObservationError(f"git {command} timed out") from exc
    except OSError as exc:
        raise ObservationError(f"Could not run git {command}: {exc}") from exc
