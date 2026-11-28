"""技能加载与匹配：读取 SKILL.md，默认提供简短索引，输入命中触发词时提供正文。

支持带 frontmatter 的技能文件，也可从普通 Markdown 标题和首段提取名称与描述。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_INLINE_ITEM = re.compile(r"""\s*("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^,]+)""")


@dataclass(frozen=True)
class _Skill:
    """解析后的单项技能，保存名称、描述、规范化触发词和待注入的正文。"""

    name: str
    description: str
    triggers: tuple[str, ...]
    body: str


class Skills:
    """启动时读取指定文件或目录中的技能，按用户输入匹配需要注入上下文的正文。

    index() 仅返回截短的名称和描述；match_input() 不区分大小写地匹配触发词。
    """

    def __init__(self, path: Path):
        path = Path(path)
        files = [path] if path.is_file() else sorted(path.rglob("SKILL.md"))
        self._skills = [_read_skill(file) for file in files]

    def index(self) -> str:
        return "\n".join(
            f"- {skill.name[:80]}: {skill.description[:160]}"
            if skill.description
            else f"- {skill.name[:80]}"
            for skill in self._skills
        )

    def match_input(self, message: str) -> list[tuple[str, str]]:
        folded = message.casefold()
        return [
            (skill.name, skill.body)
            for skill in self._skills
            if any(trigger in folded for trigger in skill.triggers)
        ]


def _read_skill(path: Path) -> _Skill:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if lines and lines[0].strip() == "---":
        end = next(
            (i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None
        )
        if end is not None:
            return _read_frontmatter(lines[1:end], lines[end + 1 :], path.parent.name)

    body = "\n".join(lines).strip()
    intro = [line.strip() for line in lines if line.strip()]
    name = path.parent.name
    if intro and intro[0].startswith("# "):
        name = intro.pop(0)[2:].strip() or name
    description = intro[0] if intro and not intro[0].startswith("#") else ""
    return _Skill(name, description, (name.casefold(),), body)


def _read_frontmatter(header: list[str], body: list[str], default_name: str) -> _Skill:
    fields: dict[str, str] = {}
    items: dict[str, list[str]] = {}
    current = ""
    for line in header:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if current and stripped.startswith("- "):
            items.setdefault(current, []).append(_scalar(stripped[2:]))
        elif current and line[0].isspace():
            items.setdefault(current, []).append(stripped)
        else:
            current, _, value = stripped.partition(":")
            fields[current] = value.strip()

    name = _scalar(fields.get("name", "")) or default_name
    description = fields.get("description", "")
    if description in {">", ">-", ">+", "|", "|-", "|+"}:
        description = " ".join(items.get("description", []))
    description = " ".join(_scalar(description).split())

    value = fields.get("triggers", "")
    triggers = items.get("triggers", [])
    if value.startswith("[") and value.endswith("]"):
        triggers = [
            _scalar(match.group()) for match in _INLINE_ITEM.finditer(value[1:-1])
        ]
    elif value:
        triggers = [_scalar(value)]
    normalized = tuple(trigger.casefold() for trigger in triggers if trigger)
    return _Skill(
        name, description, normalized or (name.casefold(),), "\n".join(body).strip()
    )


def _scalar(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value
