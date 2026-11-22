"""Common Git patch options and delivery evidence."""

import hashlib
import re

PATCH_OPTIONS = (
    "--no-ext-diff", "--no-textconv", "--no-renames", "--no-relative",
    "--no-color", "--ignore-submodules=none", "--submodule=short",
    "--src-prefix=a/", "--dst-prefix=b/", "--unified=2", "--full-index",
)
_FILE_HEADER = re.compile(r"^diff --git ", re.MULTILINE)
_FULL_INDEX = re.compile(
    r"^index [0-9a-f]{40}(?:[0-9a-f]{24})?\.\.[0-9a-f]{40}(?:[0-9a-f]{24})?"
    r"(?: [0-7]{6})?$",
    re.MULTILINE,
)


def change_fingerprint(patch: str) -> str | None:
    """Identify a complete set of Git path changes with before and after object IDs."""
    starts = [match.start() for match in _FILE_HEADER.finditer(patch)]
    if not starts or starts[0] != 0 or not patch.endswith("\n"):
        return None
    sections = [patch[start:end] for start, end in zip(starts, starts[1:] + [len(patch)])]
    if any(not _FULL_INDEX.search(section) for section in sections):
        return None
    return hashlib.sha256("\0".join(sorted(sections)).encode()).hexdigest()
