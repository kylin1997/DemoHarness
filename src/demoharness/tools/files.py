"""Task workspaces, bounded file tools, and typed acceptance checks."""

from __future__ import annotations

import ast
import asyncio
import difflib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ..core.blocking import run_io
from ..core.git import PATCH_OPTIONS
from ..core.task import Action, CheckEvidence, FileCheck, Task, Verdict
from ..core.tools import (
    ReplayPolicy,
    ToolRejected,
    ToolResult,
    ToolSpec,
    UnknownEffect,
    validate_params,
)

MAX_FILE_BYTES = 1024 * 1024
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class WorkspaceError(ToolRejected):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(path: str) -> tuple[str, ...]:
    parts = PurePosixPath(path).parts
    if (
        not parts
        or any(part.casefold() in {"/", "//", "..", ".git"} for part in parts)
        or "\0" in path
    ):
        raise WorkspaceError(f"Path outside the file scope: {path!r}")
    return parts


def _git(root: Path, lock_fd: int, *args: str, allowed: tuple[int, ...] = (0,),
         errors: str = "replace") -> str:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_OPTIONAL_LOCKS="0",
    )
    flags = [
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "submodule.recurse=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.quotePath=true",
    ]

    def run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(root), *flags, *arguments],
            check=False,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors=errors,
            timeout=30,
            pass_fds=(lock_fd,),
        )

    # Checkout, status and diff can invoke a repository's configured filters.
    if args[0] in {"worktree", "status", "diff"}:
        config = run(("config", "--name-only", "--get-regexp", r"^filter\."))
        if config.returncode not in (0, 1):
            raise WorkspaceError(config.stderr.strip())
        filters = {key.rsplit(".", 1)[0] for key in config.stdout.splitlines()}
        for name in sorted(filters):
            flags.extend(
                (
                    "-c",
                    f"{name}.process=",
                    "-c",
                    f"{name}.clean=",
                    "-c",
                    f"{name}.smudge=",
                    "-c",
                    f"{name}.required=false",
                )
            )
    result = run(args)
    if result.returncode not in allowed:
        raise WorkspaceError(
            result.stderr.strip() or f"git {args[0]} exited with {result.returncode}"
        )
    return result.stdout


class Workspace:
    def __init__(self, task: Task, state: Path, lock_fd: int):
        self.task = task
        self.root = task.workspace
        self.state = state
        self.lock_fd = lock_fd
        self.baseline = state / "baselines" / task.id

    def describe(self) -> tuple[str, str | None]:
        if not self.task.project.is_dir():
            raise WorkspaceError(f"Project directory missing: {self.task.project}")
        if os.path.lexists(self.task.project / ".git"):
            return "git", _git(
                self.task.project,
                self.lock_fd,
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ).strip()
        return "copy", None

    def prepare(self) -> None:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        if self.root.parent.is_symlink():
            raise WorkspaceError("Workspace directory is a symbolic link")
        existing = os.path.lexists(self.root)
        if self.task.workspace_kind == "git":
            if not existing:
                _git(
                    self.task.project,
                    self.lock_fd,
                    "worktree",
                    "add",
                    "--detach",
                    str(self.root),
                    self.task.base_commit,
                )
            self.validate()
            if existing and self._git_status():
                raise WorkspaceError(
                    "Interrupted worktree preparation left changes that require inspection"
                )
            return
        if not self.baseline.exists():
            self._copy(self.task.project, self.baseline, exclude_state=True)
        if not existing:
            self._copy(self.baseline, self.root)
        self.validate()

    def _copy(
        self, source: Path, destination: Path, *, exclude_state: bool = False
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = destination.with_name(f".copy-{destination.name}")
        if stage.exists():
            shutil.rmtree(stage)

        def ignore(directory: str, names: list[str]) -> list[str]:
            return [
                name
                for name in names
                if name.casefold() == ".git"
                or (exclude_state and Path(directory) / name == self.state)
            ]

        shutil.copytree(source, stage, symlinks=True, ignore=ignore)
        stage.rename(destination)

    def validate(self) -> None:
        with self._directory(()) as _:
            pass
        if self.task.workspace_kind == "git":
            source = _git(
                self.task.project,
                self.lock_fd,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ).strip()
            details = _git(
                self.root,
                self.lock_fd,
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--git-common-dir",
                "HEAD",
            ).splitlines()
            if details != [str(self.root), source, self.task.base_commit]:
                raise WorkspaceError(
                    "Worktree no longer matches the Task's project and base commit"
                )
        elif not self.baseline.is_dir() or self.baseline.is_symlink():
            raise WorkspaceError("The Task's baseline copy is missing")

    @contextmanager
    def _directory(
        self, parts: tuple[str, ...], *, create: bool = False
    ) -> Iterator[int]:
        fd = os.open(self.root.parent, _DIRECTORY)
        try:
            for index, part in enumerate((self.root.name, *parts)):
                try:
                    child = os.open(part, _DIRECTORY, dir_fd=fd)
                except FileNotFoundError:
                    if not (create and index):
                        raise
                    os.mkdir(part, dir_fd=fd)
                    child = os.open(part, _DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def read(self, path: str) -> bytes:
        *parents, name = _relative(path)
        with self._directory(tuple(parents)) as parent:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WorkspaceError(f"Expected an unlinked regular file at {path!r}")
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkspaceError(
                        f"Expected an unlinked regular file at {path!r}"
                    )
                data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise WorkspaceError(
                        f"File exceeds {MAX_FILE_BYTES} bytes: {path!r}"
                    )
                return data

    def fingerprint(self, path: str) -> str | None:
        try:
            return _sha(self.read(path))
        except FileNotFoundError:
            return None

    def write(self, path: str, data: bytes, before: str | None) -> None:
        *parents, name = _relative(path)
        if self.fingerprint(path) != before:
            raise UnknownEffect(f"{path!r} changed before the write")
        with self._directory(tuple(parents), create=True) as parent:
            mode = 0o644
            if before is not None:
                mode = (
                    stat.S_IMODE(
                        os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
                    )
                    & 0o777
                )
            staging = self.state / "staging"
            staging.mkdir(exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=staging)
            try:
                with os.fdopen(fd, "wb") as stream:
                    os.fchmod(stream.fileno(), mode)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, name, dst_dir_fd=parent)
                try:
                    os.fsync(parent)
                except OSError as error:
                    raise UnknownEffect(f"Could not sync {path!r}: {error}") from error
            finally:
                Path(temp).unlink(missing_ok=True)

    def _git_status(self) -> str:
        return _git(
            self.root,
            self.lock_fd,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=all",
        )

    def diff(self) -> str:
        if self.task.workspace_kind == "git":
            diff = _git(
                self.root,
                self.lock_fd,
                "diff",
                *PATCH_OPTIONS,
                "HEAD",
                "--",
            )
            added = _git(self.root, self.lock_fd, "ls-files", "--others", "-z", errors="surrogateescape")
            for path in added.split("\0"):
                if path:
                    diff += _git(
                        self.root,
                        self.lock_fd,
                        "diff",
                        "--no-index",
                        *PATCH_OPTIONS,
                        "--",
                        os.devnull,
                        path,
                        allowed=(0, 1),
                    )
            return diff
        return _copy_diff(self.baseline, self.root)


def _copy_diff(baseline: Path, workspace: Path) -> str:
    def files(root: Path) -> set[Path]:
        return {
            (Path(directory) / name).relative_to(root)
            for directory, dirs, names in os.walk(root)
            for name in names + dirs
            if not (Path(directory) / name).is_dir()
            or (Path(directory) / name).is_symlink()
        }

    def content(root: Path, name: Path) -> list[str]:
        path = root / name
        if path.is_symlink():
            return [os.readlink(path) + "\n"]
        if path.is_file():
            with path.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise WorkspaceError(f"File exceeds {MAX_FILE_BYTES} bytes: {name!s}")
            return data.decode("utf-8", errors="replace").splitlines(keepends=True)
        return []

    changes = []
    original, current = files(baseline), files(workspace)
    for name in sorted(original | current):
        old = content(baseline, name) if name in original else []
        new = content(workspace, name) if name in current else []
        changes.extend(
            difflib.unified_diff(
                old,
                new,
                f"a/{name}" if name in original else "/dev/null",
                f"b/{name}" if name in current else "/dev/null",
            )
        )
    return "".join(changes)


_STRING = {"type": "string"}


def _spec(
    name: str,
    description: str,
    fields: tuple[str, ...],
    replay: ReplayPolicy = ReplayPolicy.SAFE,
) -> ToolSpec:
    return ToolSpec(
        name,
        description,
        {
            "type": "object",
            "properties": dict.fromkeys(fields, _STRING),
            "required": list(fields),
        },
        replay=replay,
    )


_SPECS = {
    "read_file": _spec(
        "read_file", "Read a UTF-8 file in the Task workspace.", ("path",)
    ),
    "write_file": _spec(
        "write_file",
        "Replace a UTF-8 file in the Task workspace.",
        ("path", "content"),
        ReplayPolicy.NEVER,
    ),
    "run_check": _spec(
        "run_check",
        "Run a typed acceptance check from the Task contract.",
        ("recipe_id",),
    ),
}

FILE_CAPABILITIES = frozenset(_SPECS)


class FileTools:
    def __init__(
        self,
        workspace: Workspace,
        capabilities: frozenset[str],
        acceptance: tuple[FileCheck, ...],
    ):
        self.workspace = workspace
        self.capabilities = capabilities
        self.checks = {check.id: check for check in acceptance}

    def definitions(self) -> list[ToolSpec]:
        return [spec for name, spec in _SPECS.items() if name in self.capabilities]

    def authorize(self, action: Action) -> None:
        call = action.call
        if call.name not in self.capabilities or call.name not in _SPECS:
            raise ToolRejected(f"Tool is not authorized: {call.name}")
        valid, detail = validate_params(_SPECS[call.name], call.params)
        if not valid:
            raise ToolRejected(detail)
        if call.name == "run_check" and call.params["recipe_id"] not in self.checks:
            raise ToolRejected(f"Unknown check recipe: {call.params['recipe_id']}")

    async def prepare(self, action: Action) -> dict | None:
        self.authorize(action)
        if action.call.name == "write_file":
            return await run_io(self._write_intent, action.call.params)
        return None

    def _write_intent(self, params: dict) -> dict:
        content = params["content"].encode("utf-8")
        if len(content) > MAX_FILE_BYTES:
            raise ToolRejected(f"File exceeds {MAX_FILE_BYTES} bytes")
        return {
            "before": self.workspace.fingerprint(params["path"]),
            "after": _sha(content),
        }

    async def reconcile(self, action: Action) -> ToolResult | None:
        spec = _SPECS.get(action.call.name)
        if spec and spec.replay is ReplayPolicy.SAFE:
            return None
        if action.call.name != "write_file" or action.intent is None:
            raise UnknownEffect(f"Cannot reconcile {action.call.name} ({action.id})")
        path = action.call.params["path"]
        try:
            actual = await run_io(self.workspace.fingerprint, path)
        except (OSError, WorkspaceError) as error:
            raise UnknownEffect(f"Cannot reconcile {path!r}: {error}") from error
        if actual == action.intent["after"]:
            return _written(path, action.intent, recovered=True)
        if actual == action.intent["before"]:
            return None
        raise UnknownEffect(
            f"{path!r} matches neither the original nor the intended fingerprint"
        )

    async def execute(
        self, action: Action, intent: dict | None, cancel: asyncio.Event
    ) -> ToolResult:
        return await run_io(self._execute_file, action, intent)

    def _execute_file(self, action: Action, intent: dict | None) -> ToolResult:
        name, params = action.call.name, action.call.params
        if name == "read_file":
            data = self.workspace.read(params["path"])
            return ToolResult(data.decode("utf-8"), {"sha256": _sha(data)})
        if name == "write_file":
            self.workspace.write(
                params["path"], params["content"].encode("utf-8"), intent["before"]
            )
            return _written(params["path"], intent)
        evidence = self.check(self.checks[params["recipe_id"]])
        return ToolResult(
            json.dumps(vars(evidence), ensure_ascii=False),
            vars(evidence),
            evidence.verdict is not Verdict.PASSED,
        )

    def check(self, check: FileCheck) -> CheckEvidence:
        if "run_check" not in self.capabilities:
            return CheckEvidence(
                check.id,
                check.path,
                Verdict.UNVERIFIABLE,
                "Check capability is unavailable",
            )
        digest = None
        try:
            data = self.workspace.read(check.path)
            digest = _sha(data)
            if check.equals is not None and data != check.equals.encode("utf-8"):
                return CheckEvidence(
                    check.id, check.path, Verdict.FAILED, "File contents differ", digest
                )
            if (
                check.contains is not None
                and check.contains.encode("utf-8") not in data
            ):
                return CheckEvidence(
                    check.id,
                    check.path,
                    Verdict.FAILED,
                    "Required text is missing",
                    digest,
                )
            if check.python_syntax:
                ast.parse(data, filename=check.path)
        except (FileNotFoundError, SyntaxError) as error:
            return CheckEvidence(
                check.id, check.path, Verdict.FAILED, str(error), digest
            )
        except (OSError, WorkspaceError, UnicodeError) as error:
            return CheckEvidence(
                check.id, check.path, Verdict.UNVERIFIABLE, str(error), digest
            )
        return CheckEvidence(
            check.id, check.path, Verdict.PASSED, "Check passed", digest
        )


def _written(path: str, intent: dict, recovered: bool = False) -> ToolResult:
    return ToolResult(
        f"Wrote {path}",
        {
            "before_sha256": intent["before"],
            "after_sha256": intent["after"],
            "recovered": recovered,
        },
    )
