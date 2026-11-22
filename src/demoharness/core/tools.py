"""DemoHarness 内核 · 工具调用协议（core/tools.py）。

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

参数校验（T01.4 冻结）：手写 JSON Schema 最小子集，标量 + required +
enum/min/max + `{"type":"array","items":{"type":<scalar>}}` 的元素校验。
复合判定只针对**实际传入的参数值**，不再遍历 schema 全部属性——否则一个
声明了复合参数但本次没传的工具也会被整体拒绝（delegate_subagent 的
tools 数组即此情形）。不引入 jsonschema，保持 httpx 唯一依赖。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .task import Action


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


def is_replay_safe(spec: ToolSpec) -> bool:
    """恢复/后台续跑的统一判定接口（L2-4 的 ReplayPolicy.SAFE 消费点）。

    本轮冻结接口形状：仅显式声明 SAFE 的工具允许中断后重放；副作用工具
    一律物化中断（架构不变量 5）。后台任务的自动重排（第 8 步）据此判定。
    """
    return spec.replay is ReplayPolicy.SAFE


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


# before_tool_call 契约： (工具名, 实际入参) -> BeforeVerdict | None。
# None = 放行；block=True = 拦截并把 reason 作为错误结果返回模型（工具不执行）。
# 多个钩子按装配顺序遍历，第一个 block 即短路；钩子本身不得抛异常给模型。
BeforeToolHook = Callable[[str, dict], BeforeVerdict | None]
AfterToolHook = Callable[[ToolResult], ToolResult]
ToolHooks = BeforeToolHook  # 向后兼容别名（既有代码与测试用此名）


class Tool(Protocol):
    spec: ToolSpec

    async def execute(
        self,
        call_id: str,
        params: dict,
        cancel: asyncio.Event,
    ) -> ToolResult: ...


# ── 参数校验（手写 JSON Schema 最小子集，零新增依赖） ──

_SCALAR_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool}


def validate_params(spec: ToolSpec, params: dict) -> tuple[bool, str]:
    """按 ToolSpec.parameters 校验**实际传入**的入参，返回 (ok, reason)。

    支持：标量 (string/number/integer/boolean)、顶层 required、enum、
    number/integer 的 minimum/maximum、string 的 minLength/maxLength，以及
    `{"type":"array","items":{"type":<scalar>}}` 的最小子集（含元素类型、
    minItems/maxItems）。object/null/anyOf/oneOf/allOf/$ref 等复合 schema
    仍在显式债务内：遇到即给出可读的「不支持」错误，不静默放行。

    设计原因：复合判定放在「实际值」循环内，声明层不预扫描全部属性——
    声明了数组参数但本次不传的工具（如 delegate_subagent 只传 prompt）必须
    能到达 execute，这是 L2-5 复合参数链路的根因修法。
    """
    schema = spec.parameters or {}
    props = schema.get("properties", {})
    top_type = schema.get("type")

    # 参数本身声明为 object 是合法的（工具入参本就是个对象）。
    if top_type not in (None, "object"):
        return False, f"工具 {spec.name} 的参数 schema 顶层类型不支持：{top_type}"

    required = set(schema.get("required", []))
    missing = required - set(params.keys())
    if missing:
        return False, f"缺少必需参数：{', '.join(sorted(missing))}"

    for name, value in params.items():
        prop = props.get(name)
        if prop is None:
            return False, f"参数 {name} 不在 schema 内（多余字段）"
        ptype = prop.get("type")
        if ptype in _SCALAR_TYPES:
            ok, reason = _validate_scalar(name, ptype, value, prop)
            if not ok:
                return False, reason
        elif ptype == "array":
            ok, reason = _validate_array(name, value, prop)
            if not ok:
                return False, reason
        else:
            return False, f"参数 {name} 类型暂不支持（需复合 schema）：{ptype}"

    return True, ""


def _validate_scalar(name: str, ptype: str, value: object, prop: dict) -> tuple[bool, str]:
    """单个标量参数的校验（含 bool 不是 integer 的显式排除）。"""
    expected = _SCALAR_TYPES[ptype]
    # bool 是 int 的子类：integer 必须显式排除 bool，放 isinstance 之前判断。
    if ptype == "integer" and isinstance(value, bool):
        return False, f"参数 {name} 期望 integer，实际是 boolean"
    if not isinstance(value, expected):
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


def _validate_array(name: str, value: object, prop: dict) -> tuple[bool, str]:
    """复合 schema 的最小子集：`{"type":"array","items":{"type":<scalar>}}`。

    只接受标量 items.type；items 缺失、items 为复合声明一律明确拒绝，
    避免「声明看起来支持、实际没校验」的假通过。
    """
    if not isinstance(value, list):
        return False, f"参数 {name} 期望 array，实际是 {type(value).__name__}"
    items = prop.get("items")
    if not isinstance(items, dict) or items.get("type") not in _SCALAR_TYPES:
        return False, f"参数 {name} 的 array 元素声明不支持（需 items.type 为标量类型）"
    item_type = items["type"]
    for idx, item in enumerate(value):
        if item_type == "integer" and isinstance(item, bool):
            return False, f"参数 {name}[{idx}] 期望 integer，实际是 boolean"
        if not isinstance(item, _SCALAR_TYPES[item_type]):
            return False, (
                f"参数 {name}[{idx}] 期望 {item_type}，实际是 {type(item).__name__}"
            )
    lo, hi = prop.get("minItems"), prop.get("maxItems")
    if lo is not None and len(value) < lo:
        return False, f"参数 {name} 元素数 {len(value)} 小于下限 {lo}"
    if hi is not None and len(value) > hi:
        return False, f"参数 {name} 元素数 {len(value)} 大于上限 {hi}"
    return True, ""


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
    before_hooks: list[BeforeToolHook] | None = None,
    after_hooks: list[AfterToolHook] | None = None,
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


class ToolRejected(ValueError):
    pass


class UnknownEffect(Exception):
    pass


class RecoverableTools(Protocol):
    def definitions(self) -> list[ToolSpec]: ...

    def authorize(self, action: Action) -> None: ...

    def prepare(self, action: Action) -> dict | None: ...

    def reconcile(self, action: Action) -> ToolResult | None: ...

    async def execute(self, action: Action, intent: dict | None, cancel: asyncio.Event) -> ToolResult: ...


def model_tool_call(call_id: str, name: str, params: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(params)}}
