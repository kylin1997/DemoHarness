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
from pathlib import Path

from ..core.blocking import run_io
from ..core.git import (
    DIFF_OPTIONS,
    PATCH_OPTIONS,
    RAW_OPTIONS,
    Change,
    change_fingerprint,
    raw_changes,
)
from ..core.task import Action, Check, CheckEvidence, FileCheck, Task, Verdict, Verifier, task_path
from ..core.tools import (
    RegisteredTool,
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


class WriteScopeError(WorkspaceError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(path: str) -> tuple[str, ...]:
    try:
        return tuple(task_path(path).split("/"))
    except (TypeError, ValueError) as error:
        raise WorkspaceError(str(error)) from error


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

    def diff(self, allowed_write_paths: frozenset[str]) -> tuple[str, str | None]:
        if self.task.workspace_kind == "git":
            tracked = raw_changes(_git(self.root, self.lock_fd, "diff", *RAW_OPTIONS,
                                       self.task.base_commit, "--", errors="surrogateescape"))
            staged = _git(self.root, self.lock_fd, "diff", *DIFF_OPTIONS, "--cached", "--name-only", "-z",
                          self.task.base_commit, "--", errors="surrogateescape")
            added = _git(self.root, self.lock_fd, "ls-files", "--others", "-z", errors="surrogateescape")
            additions = [path for path in added.split("\0") if path]
            changed = {change[0] for change in tracked}
            changed.update(path for path in staged.split("\0") if path)
            changed.update(additions)
            _verify_write_scope(changed, allowed_write_paths)
            diff = _git(
                self.root,
                self.lock_fd,
                "diff",
                *PATCH_OPTIONS,
                self.task.base_commit,
                "--",
            )
            for path in additions:
                no_index = ("diff", "--no-index")
                files = ("--", os.devnull, path)
                diff += _git(self.root, self.lock_fd, *no_index, *PATCH_OPTIONS, *files, allowed=(0, 1))
                tracked.extend(raw_changes(_git(
                    self.root, self.lock_fd, *no_index, *RAW_OPTIONS, *files,
                    allowed=(0, 1), errors="surrogateescape",
                )))
            return diff, self._change_fingerprint(tracked)
        return _copy_diff(self.baseline, self.root, allowed_write_paths), None

    def _change_fingerprint(self, changes: list[Change]) -> str | None:
        def resolved() -> Iterator[Change]:
            for path, before_mode, after_mode, before_id, after_id in changes:
                if after_mode != "000000" and not after_id.strip("0"):
                    if after_mode == "120000":
                        *parents, name = _relative(path)
                        with self._directory(tuple(parents)) as parent:
                            data = os.fsencode(os.readlink(name, dir_fd=parent))
                    else:
                        data = self.read(path)
                    sha = hashlib.sha256() if len(before_id) == 64 else hashlib.sha1(usedforsecurity=False)
                    sha.update(f"blob {len(data)}\0".encode("ascii"))
                    sha.update(data)
                    after_id = sha.hexdigest()
                yield path, before_mode, after_mode, before_id, after_id

        try:
            return change_fingerprint(resolved())
        except (OSError, WorkspaceError):
            return None


def _verify_write_scope(changed: set[str], allowed: frozenset[str]) -> None:
    unexpected = changed - allowed
    if unexpected:
        raise WriteScopeError("Changes outside the Task write scope: " + ", ".join(map(repr, sorted(unexpected))))


def _copy_diff(baseline: Path, workspace: Path, allowed_write_paths: frozenset[str]) -> str:
    def walk_error(error: OSError) -> None:
        raise error

    def files(root: Path) -> dict[Path, int]:
        entries = {}
        for directory, dirs, names in os.walk(root, onerror=walk_error):
            for name in names + dirs:
                path = Path(directory) / name
                mode = path.lstat().st_mode
                if not stat.S_ISDIR(mode):
                    entries[path.relative_to(root)] = mode
        return entries

    def fingerprint(root: Path, name: Path, mode: int) -> bytes | str | None:
        path = root / name
        if stat.S_ISLNK(mode):
            return os.readlink(path)
        if stat.S_ISREG(mode):
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").digest()
        return None

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
    changed = {
        name for name in original.keys() | current.keys()
        if original.get(name) != current.get(name)
        or fingerprint(baseline, name, original[name]) != fingerprint(workspace, name, current[name])
    }
    _verify_write_scope({name.as_posix() for name in changed}, allowed_write_paths)
    for name in sorted(changed):
        old = content(baseline, name) if name in original else []
        new = content(workspace, name) if name in current else []
        before = f"a/{name}" if name in original else "/dev/null"
        after = f"b/{name}" if name in current else "/dev/null"
        changes.extend(difflib.unified_diff(old, new, before, after) if old != new
                       else [f"--- {before}\n+++ {after}\n"])
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
        acceptance: tuple[FileCheck | Check, ...],
        tools: tuple[RegisteredTool, ...] = (),
        verifiers: tuple[Verifier, ...] = (),
    ):
        self.registered = {tool.spec.name: tool for tool in tools if tool.spec.name in capabilities}
        self.verifiers = {verifier.name: verifier for verifier in verifiers}
        self.specs = {**_SPECS, **{name: tool.spec for name, tool in self.registered.items()}}
        self.workspace = workspace
        self.capabilities = capabilities
        self.checks = {check.id: check for check in acceptance}

    def definitions(self) -> list[ToolSpec]:
        return [spec for name, spec in self.specs.items() if name in self.capabilities]

    def allowed_write_paths(self) -> frozenset[str]:
        return self.workspace.task.allowed_write_paths

    def authorize(self, action: Action) -> None:
        call = action.call
        if call.name not in self.capabilities or call.name not in self.specs:
            raise ToolRejected(f"Tool is not authorized: {call.name}")
        valid, detail = validate_params(self.specs[call.name], call.params)
        if not valid:
            raise ToolRejected(detail)
        if call.name == "write_file" and "/".join(_relative(call.params["path"])) not in self.allowed_write_paths():
            raise ToolRejected(f"Path outside the Task write scope: {call.params['path']!r}")
        if call.name == "run_check":
            recipe = call.params["recipe_id"]
            if recipe not in self.checks:
                raise ToolRejected(f"Unknown check recipe: {recipe}")
            if isinstance(self.checks[recipe], Check):
                raise ToolRejected("The host runs this verifier on the final delivery text")

    async def prepare(self, action: Action) -> dict | None:
        self.authorize(action)
        if action.call.name == "write_file":
            return await run_io(self._write_intent, action.call.params)
        if action.call.name in self.registered:
            return {"replay": self.registered[action.call.name].spec.replay.value}
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
        spec = self.specs.get(action.call.name)
        if action.call.name in self.registered and (action.intent or {}).get("replay") != ReplayPolicy.SAFE.value:
            raise UnknownEffect(f"Cannot reconcile {action.call.name} ({action.id})")
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
        return await run_io(self._execute_file, action, intent, record_result_on_cancel=True)

    def _execute_file(self, action: Action, intent: dict | None) -> ToolResult:
        name, params = action.call.name, action.call.params
        if name in self.registered:
            try:
                result = self.registered[name].execute(params)
                if not isinstance(result, ToolResult) or not isinstance(result.content, str):
                    raise TypeError("Expected ToolResult with text content")
                json.dumps(result.details, allow_nan=False)
                return result
            except (ToolRejected, UnknownEffect):
                raise
            except Exception as error:
                if self.registered[name].spec.replay is ReplayPolicy.NEVER:
                    raise UnknownEffect(f"Unknown effect of {name}: {error}") from error
                raise ToolRejected(f"{name}: {error}") from error
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

    def check(self, check: FileCheck | Check, task: Task | None = None, text: str = "") -> CheckEvidence:
        if isinstance(check, Check):
            verifier = self.verifiers.get(check.verifier)
            if verifier is None:
                return CheckEvidence(check.id, None, Verdict.UNVERIFIABLE, f"Missing verifier: {check.verifier}")
            try:
                verdict, detail = verifier.verify(task or self.workspace.task, check, text)
                if not isinstance(verdict, Verdict) or not isinstance(detail, str):
                    raise ValueError("Expected (Verdict, detail)")
            except Exception as error:
                return CheckEvidence(check.id, None, Verdict.UNVERIFIABLE, f"{check.verifier}: {error}")
            return CheckEvidence(check.id, None, verdict, detail)
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
