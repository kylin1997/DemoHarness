"""plan-mode 扩展策略（strategies/plan_mode.py）。

对齐 pi 的 plan-mode 扩展（references/pi/.../plan-mode/），但只落**能力本体**，
把交互面做成接口留给 SDK 调用方（不走 TUI）。

能力本体（无 UI 的纯逻辑）：
- 工具约束：只读命令白名单（is_safe_command）——危险命令在 plan 模式下被 before_hook 拦截
- 计划提取：extract_todo_items 从「Plan:」段落提取编号步骤
- 进度标记：[DONE:n] → mark_completed_steps
- 状态机：enter / execute / reset（用户确认执行是接口方法，由 UI 层触发）

状态持久化：plan 的 todo 列表作为策略状态，由策略层持有；跨 turn 存活由
BotRuntime 持有策略实例来保证（教学规模，进程内即可），不落 entry（与 pi 的
"落 session entry" 有一处差异——本阶段策略状态进程内持有足够，见决策注释）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from demoharness.core.tools import BeforeVerdict

# ── 只读命令白名单（照 pi utils.ts 直译） ──

_DESTRUCTIVE_PATTERNS = [
    r"\brm\b", r"\brmdir\b", r"\bmv\b", r"\bcp\b", r"\bmkdir\b", r"\btouch\b",
    r"\bchmod\b", r"\bchown\b", r"\bchgrp\b", r"\bln\b", r"\btee\b",
    r"\btruncate\b", r"\bdd\b", r"\bshred\b", r"(^|[^<])>(?!>)", r">>",
    r"\bnpm\s+(install|uninstall|update|ci|link|publish)",
    r"\byarn\s+(add|remove|install|publish)",
    r"\bpnpm\s+(add|remove|install|publish)",
    r"\bpip\s+(install|uninstall)",
    r"\bapt(-get)?\s+(install|remove|purge|update|upgrade)",
    r"\bbrew\s+(install|uninstall|upgrade)",
    r"\bgit\s+(add|commit|push|pull|merge|rebase|reset|checkout|branch\s+-[dD]|stash|cherry-pick|revert|tag|init|clone)",
    r"\bsudo\b", r"\bsu\b", r"\bkill\b", r"\bpkill\b", r"\bkillall\b",
    r"\breboot\b", r"\bshutdown\b",
    r"\bsystemctl\s+(start|stop|restart|enable|disable)",
    r"\bservice\s+\S+\s+(start|stop|restart)",
    r"\b(vim?|nano|emacs|code|subl)\b",
]

_SAFE_PATTERNS = [
    r"^\s*cat\b", r"^\s*head\b", r"^\s*tail\b", r"^\s*less\b", r"^\s*more\b",
    r"^\s*grep\b", r"^\s*find\b", r"^\s*ls\b", r"^\s*pwd\b", r"^\s*echo\b",
    r"^\s*printf\b", r"^\s*wc\b", r"^\s*sort\b", r"^\s*uniq\b", r"^\s*diff\b",
    r"^\s*file\b", r"^\s*stat\b", r"^\s*du\b", r"^\s*df\b", r"^\s*tree\b",
    r"^\s*which\b", r"^\s*whereis\b", r"^\s*type\b", r"^\s*env\b",
    r"^\s*printenv\b", r"^\s*uname\b", r"^\s*whoami\b", r"^\s*id\b",
    r"^\s*date\b", r"^\s*cal\b", r"^\s*uptime\b", r"^\s*ps\b", r"^\s*top\b",
    r"^\s*htop\b", r"^\s*free\b",
    r"^\s*git\s+(status|log|diff|show|branch|remote|config\s+--get)",
    r"^\s*git\s+ls-", r"^\s*npm\s+(list|ls|view|info|search|outdated|audit)",
    r"^\s*yarn\s+(list|info|why|audit)", r"^\s*node\s+--version",
    r"^\s*python\s+--version", r"^\s*curl\s", r"^\s*wget\s+-O\s*-",
    r"^\s*jq\b", r"^\s*sed\s+-n", r"^\s*awk\b", r"^\s*rg\b", r"^\s*fd\b",
    r"^\s*bat\b", r"^\s*eza\b",
]


def is_safe_command(command: str) -> bool:
    """run_command 的命令在 plan 模式下是否安全（只读）。"""
    was_destructive = any(re.search(p, command, re.IGNORECASE) for p in _DESTRUCTIVE_PATTERNS)
    was_safe = any(re.search(p, command, re.IGNORECASE) for p in _SAFE_PATTERNS)
    return (not was_destructive) and was_safe


# ── 计划提取（照 pi utils.ts 直译） ──


@dataclass
class TodoItem:
    step: int
    text: str
    completed: bool = False


def _clean_step_text(text: str) -> str:
    cleaned = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)   # 去粗/斜体
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)            # 去代码
    cleaned = re.sub(
        r"^(Use|Run|Execute|Create|Write|Read|Check|Verify|Update|Modify|Add|Remove|Delete|Install)\s+(the\s+)?",
        "", cleaned, flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    if len(cleaned) > 50:
        cleaned = f"{cleaned[:47]}..."
    return cleaned


def extract_todo_items(message: str) -> list[TodoItem]:
    """从「Plan:」段落提取编号步骤（1. / 1) 等）。"""
    header = re.search(r"\*{0,2}Plan:\*{0,2}\s*\n", message, re.IGNORECASE)
    if not header:
        return []
    section = message[header.end():]
    items: list[TodoItem] = []
    for m in re.finditer(r"^\s*(\d+)[.)]\s+\*{0,2}([^*\n]+)", section, re.MULTILINE):
        text = m.group(2).strip()
        text = re.sub(r"\*{1,2}$", "", text).strip()
        if len(text) > 5 and not text.startswith("`") and not text.startswith("/") and not text.startswith("-"):
            cleaned = _clean_step_text(text)
            if len(cleaned) > 3:
                items.append(TodoItem(step=len(items) + 1, text=cleaned))
    return items


def extract_done_steps(message: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"\[DONE:(\d+)\]", message, re.IGNORECASE)]


def mark_completed_steps(text: str, items: list[TodoItem]) -> int:
    done = extract_done_steps(text)
    for step in done:
        item = next((t for t in items if t.step == step), None)
        if item:
            item.completed = True
    return len(done)


# ── plan-mode 策略：状态机 + 接口 ──


@dataclass
class PlanMode:
    """plan-mode 扩展策略。能力本体 + 接口，交互面由 SDK 调用方调这些方法。

    状态机：disabled → plan（只读 + 产计划）→ execute（执行计划）→ done/reset。
    - enter()  进入 plan——启用只读约束（工具白名单切换 + run_command 只读）
    - execute() 用户确认执行计划（由 UI 层触发，非 TUI）
    - reset()  退出/重置
    - before_hook 属性：挂进 LoopRuntime.before_hooks 的工具约束
    """

    enabled: bool = False
    _todos: list[TodoItem] = field(default_factory=list)

    # ── 工具约束接口：挂进 before_hooks 的约束函数 ──
    @property
    def before_hook(self):
        def _constrain(name: str, params: dict) -> BeforeVerdict | None:
            if not self.enabled:
                return None
            # plan 模式下禁用写类与 run_command（危险命令）
            if name in ("write_file",):
                return BeforeVerdict(block=True, reason="plan 模式：只读探索，已禁用 write_file")
            if name == "run_command":
                cmd = params.get("command", "")
                if not is_safe_command(cmd):
                    return BeforeVerdict(block=True, reason="plan 模式：命令非只读白名单，已拦截")
            return None
        return _constrain

    # ── 状态机接口 ──
    def enter(self) -> None:
        self.enabled = True
        self._todos = []

    def execute(self) -> None:
        """用户确认执行计划（UI 层触发）；退出只读、进入执行。"""
        self.enabled = False

    def reset(self) -> None:
        self.enabled = False
        self._todos = []

    # ── 计划接口 ──
    def load_plan(self, text: str) -> list[TodoItem]:
        """从模型输出提取计划条目（finish_turn / 每轮结束后由 loop 或 UI 调用）。"""
        items = extract_todo_items(text)
        if items:
            self._todos = items
        return self._todos

    def mark_done(self, text: str) -> int:
        """标记 [DONE:n] 完成，返回本次完成数。"""
        return mark_completed_steps(text, self._todos)

    @property
    def todos(self) -> list[TodoItem]:
        return list(self._todos)

    @property
    def remaining(self) -> list[TodoItem]:
        return [t for t in self._todos if not t.completed]
