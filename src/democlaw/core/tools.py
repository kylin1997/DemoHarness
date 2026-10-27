"""DemoClaw 内核 · 工具调用协议（core/tools.py）。

内核只定协议，不实现任何具体工具——具体工具在 tools/ 扩展目录（第 3 步
首批教学工具 read_file/write_file/run_command）。

三层抽象（对齐 pi 的 Tool/ToolCall/AgentToolResult，Python 化，见
docs/design/tools.md）：

- 声明层 ToolSpec：给模型看的 schema + replay/execution 元数据。
- 调用载体 ToolCall：call_id 是 tool_call/tool_result entry 的配对键
  （并发批次结果落盘顺序≠调用顺序，不能靠相邻顺序关联）。
- 执行层 Tool（Protocol）：Python 侧实现。
- 结果层 ToolResult：失败编码进 is_error，不抛异常。

三段式执行管线：prepare（查注册表 + 参数校验 + before 钩子）→ execute
（并发策略：任一 SEQUENTIAL 整批串行，全 PARALLEL 才 gather）→ finalize
（after 钩子可替换结果）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ReplayPolicy(Enum):
    """中断后该工具是否可安全重放（恢复用，架构不变量 5 的判定依据）。"""

    NEVER = "never"   # 副作用工具：恢复时物化中断
    SAFE = "safe"     # 只读工具：可重放（后台任务自动续跑用）


class ExecMode(Enum):
    PARALLEL = "parallel"      # 可与其他工具并发
    SEQUENTIAL = "sequential"  # 必须串行（批内有它则整批串行）


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict                 # JSON Schema（手写 dict，零依赖）
    replay: ReplayPolicy = ReplayPolicy.NEVER
    execution: ExecMode = ExecMode.PARALLEL


@dataclass(frozen=True)
class ToolCall:
    """调用载体：恢复与并发落盘的地基。"""

    call_id: str
    name: str
    params: dict


@dataclass
class ToolResult:
    content: str                  # 模型看到的结果文本（第一阶段仅文本）
    details: dict | None = None   # 结构化详情（退出码/时长等），落盘不进程上下文
    is_error: bool = False        # 失败编码，不抛异常


@dataclass(frozen=True)
class BeforeVerdict:
    """before_tool_call 钩子的裁决：block=True 时 reason 进 content 返回模型。"""

    block: bool
    reason: str


# ToolHooks：before 钩子 (name, params) -> BeforeVerdict | None（None=放行）；
# after 钩子 ToolResult -> ToolResult（可替换结果）。
ToolHooks = Callable[[str, dict], BeforeVerdict | None]


class Tool(Protocol):
    spec: ToolSpec

    async def execute(
        self,
        call_id: str,
        params: dict,
        cancel: asyncio.Event,
    ) -> ToolResult: ...


# ── 参数校验（手写，只支持标量；复合 schema 拒绝并给可读错误） ──

_SCALAR_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool}

_COMPLEX_MARKER = {"array", "object", "null", "anyOf", "oneOf", "allOf", "$ref"}


def validate_params(spec: ToolSpec, params: dict) -> tuple[bool, str]:
    """按 ToolSpec.parameters（JSON Schema 子集）校验入参。

    返回 (ok, reason)。支持标量 (string/number/integer/boolean) + 顶层
    required/enum/min/max（number+integer 的 min/max）；遇到 array/object
    等复合 schema 一律拒绝并报“暂不支持”。这是手写校验器的显式债务——
    首个需要复合参数的工具出现时引 jsonschema 库替换（tools.md 决策 1）。
    """
    schema = spec.parameters or {}
    props = schema.get("properties", {})
    top_type = schema.get("type")

    # 复合 schema 拒绝：参数本身声明为 object 是合法的（工具入参本就是个对象），
    # 但属性中的复合类型不支持。
    if top_type not in (None, "object"):
        return False, f"工具 {spec.name} 的参数 schema 顶层类型不支持：{top_type}"

    for name, prop in props.items():
        if _is_complex(prop):
            return False, f"工具 {spec.name} 的参数 {name} 类型暂不支持（需复合 schema）"

    required = set(schema.get("required", []))
    missing = required - set(params.keys())
    if missing:
        return False, f"缺少必需参数：{', '.join(sorted(missing))}"

    for name, value in params.items():
        prop = props.get(name)
        if prop is None:
            return False, f"参数 {name} 不在 schema 内（多余字段）"
        ptype = prop.get("type")
        if ptype not in _SCALAR_TYPES:
            return False, f"参数 {name} 类型暂不支持：{ptype}"
        # bool 是 int 的子类：integer 必须显式排除 bool，放 isinstance 之前判断。
        if ptype == "integer" and isinstance(value, bool):
            return False, f"参数 {name} 期望 integer，实际是 boolean"
        if not isinstance(value, _SCALAR_TYPES[ptype]):
            return False, f"参数 {name} 期望 {ptype}，实际是 {type(value).__name__}"
        if "enum" in prop and value not in prop["enum"]:
            return False, f"参数 {name} 值 {value!r} 不在允许集合 {prop['enum']!r}"
        if isinstance(value, int) and not isinstance(value, bool):
            lo, hi = prop.get("minimum"), prop.get("maximum")
            if lo is not None and value < lo:
                return False, f"参数 {name} 值 {value} 小于下限 {lo}"
            if hi is not None and value > hi:
                return False, f"参数 {name} 值 {value} 大于上限 {hi}"
        if isinstance(value, str):
            lo, hi = prop.get("minLength"), prop.get("maxLength")
            if lo is not None and len(value) < lo:
                return False, f"参数 {name} 长度 {len(value)} 小于下限 {lo}"
            if hi is not None and len(value) > hi:
                return False, f"参数 {name} 长度 {len(value)} 大于上限 {hi}"

    return True, ""


def _is_complex(prop: dict) -> bool:
    pt = prop.get("type")
    if pt in ("array", "object", "null"):
        return True
    for key in _COMPLEX_MARKER:
        if key in prop:
            return True
    return False


def _error(message: str) -> ToolResult:
    return ToolResult(content=message, is_error=True)


# ── 注册表 ──


class ToolRegistry:
    def register(self, tool: Tool) -> None:
        """装配期调用；重名抛错。"""
        if tool.spec.name in self._tools:
            raise ValueError(f"工具重名：{tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[ToolSpec]:
        """给 stream_fn 的工具声明清单，按 name 稳定排序（prompt cache 友好）。"""
        return sorted(
            (t.spec for t in self._tools.values()), key=lambda s: s.name
        )

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}


# ── 三段式执行管线 ──


async def execute_tool_calls(
    calls: list[ToolCall],
    registry: ToolRegistry,
    before_hooks: list[ToolHooks] | None = None,
    after_hooks: list[Callable[[ToolResult], ToolResult]] | None = None,
    cancel: asyncio.Event | None = None,
) -> list[ToolResult]:
    """对一批 ToolCall 执行三段式：prepare 串行 → execute → finalize 串行。

    返回与 calls 同序、等长的 ToolResult 列表。任何工具失败都不向上抛，
    编码为 is_error 结果。

    - prepare 串行：查注册表（未注册→error）→ 参数校验 → before 钩子
      （block→is_error 结果，reason 进 content）。
    - execute：并发策略（任一 SEQUENTIAL → 整批串行按原序；全 PARALLEL 才
      gather）；记录 duration 进 details。
    - finalize 串行：after 钩子可替换每个 ToolResult。
    """
    before_hooks = before_hooks or []
    after_hooks = after_hooks or []
    cancel = cancel or asyncio.Event()

    # 1) prepare 串行
    prepared: list[tuple[ToolCall, ToolResult | None]] = []
    for call in calls:
        tool = registry.get(call.name)
        if tool is None:
            prepared.append((call, _error(f"工具不存在：{call.name}")))
            continue
        ok, reason = validate_params(tool.spec, call.params)
        if not ok:
            prepared.append((call, _error(reason)))
            continue
        blocked = False
        for hook in before_hooks:
            verdict = hook(call.name, call.params)
            if verdict is not None and verdict.block:
                prepared.append((call, _error(verdict.reason)))
                blocked = True
                break
        if blocked:
            continue
        prepared.append((call, None))

    # 2) execute
    results: list[ToolResult | None] = [None] * len(calls)
    for i, (_, pre) in enumerate(prepared):
        if pre is not None:
            results[i] = pre

    to_run = [(i, c) for i, (c, _) in enumerate(prepared) if results[i] is None]
    if any(
        (t := registry.get(c.name)) is not None
        and t.spec.execution == ExecMode.SEQUENTIAL
        for c in calls
    ):
        for i, c in to_run:
            results[i] = await _execute_one(registry, cancel, c)
    else:
        outs = await asyncio.gather(*(_execute_one(registry, cancel, c) for _, c in to_run))
        for (i, _), out in zip(to_run, outs):
            results[i] = out

    # 3) finalize 串行：after 钩子可替换
    finalized: list[ToolResult] = []
    for r in results:
        assert r is not None
        for hook in after_hooks:
            r = hook(r)
        finalized.append(r)
    return finalized


async def _execute_one(registry: ToolRegistry, cancel: asyncio.Event, call: ToolCall) -> ToolResult:
    tool = registry.get(call.name)
    if tool is None:
        return _error(f"工具不存在：{call.name}")
    if cancel.is_set():
        return _error("任务已取消，工具未执行")
    start = time.monotonic()
    try:
        result = await tool.execute(call.call_id, call.params, cancel)
    except Exception as exc:  # noqa: BLE001 —— 工具异常编码进结果，不向上抛
        result = _error(f"工具执行异常：{exc}")
    if result.details is None:
        result.details = {}
    result.details["duration"] = round(time.monotonic() - start, 4)
    return result