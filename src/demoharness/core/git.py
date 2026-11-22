"""Shared Git change metadata for delivery and observation."""

import hashlib
from collections.abc import Iterable

DIFF_OPTIONS = ("--no-renames", "--no-relative", "--ignore-submodules=none")
RAW_OPTIONS = (*DIFF_OPTIONS, "--raw", "-z", "--no-abbrev")
PATCH_OPTIONS = (
    *DIFF_OPTIONS, "--no-ext-diff", "--no-textconv", "--no-color", "--submodule=short",
    "--src-prefix=a/", "--dst-prefix=b/", "--unified=2", "--full-index",
)

Change = tuple[str, str, str, str, str]


def raw_changes(raw: str) -> list[Change]:
    fields = raw.split("\0")
    changes = []
    for header, path in zip(fields[0:-1:2], fields[1:-1:2], strict=True):
        before_mode, after_mode, before_id, after_id, _ = header.removeprefix(":").split()
        changes.append((path, before_mode, after_mode, before_id, after_id))
    return changes


def change_fingerprint(changes: Iterable[Change]) -> str | None:
    records = sorted(changes)
    if not records:
        return None
    wire = "\0".join(field for change in records for field in change)
    return hashlib.sha256(wire.encode("utf-8", "surrogateescape")).hexdigest()
