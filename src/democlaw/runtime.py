"""DemoClaw · BotRuntime 组合根（runtime.py）。

唯一知道全部部件的地方；外部（Web、测试、教学示例）一律经本模块的 API
使用 DemoClaw，不直接 import core/ 细节。

门面契约（见 docs/design/botruntime-api.md）：

- 两个形态：同步（run/result，禁止在 async 语境调用）与 async（arun/aresult/
  events）。同步形态经 BotRuntime 自持的事件循环线程调度。
- 事件循环由 BotRuntime 拥有：一个后台线程跑 asyncio 循环，同步调用用
  run_coroutine_threadsafe 桥接过去。
- Bot 是无状态视图：同名多次取等价，不持运行态。
- steer 不要求调用方判断状态：运行中 → 注入当前任务；已结束 → 追加为该
  会话的下一次输入（follow-up）。

run 不传 session → 自动新建；会话延续显式传 session_id。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.events import EventBus
from .core.loop import LoopRuntime, run_task_loop
from .core.session import (
    AppendOp,
    DeleteOp,
    ForkOp,
    HistoryOp,
    NewOp,
    ResetOp,
    SessionInfo,
    SessionManager,
)
from .core.storage import EntryRow, SessionRow, Storage
from .core.task import (
    StopReason,
    is_terminal_entry,
    scan_and_recover,
    stop_reason_of,
)
from .core.tools import Tool, ToolRegistry

# ── 公开类型（形状见 botruntime-api.md） ──


class TaskPhase(str):  # 简化为字符串，形状与 botruntime-api 的 Enum 对齐
    QUEUED = "queued"
    AWAITING_TOOLS = "awaiting_tools"
    TOOLS_COMPLETED = "tools_completed"
    FINAL_RESPONSE = "final_response"


@dataclass(frozen=True)
class TaskResult:
    stop_reason: StopReason
    content: str
    error: str | None
    task_id: str
    session_id: str
    iterations: int
    tools_used: int


@dataclass(frozen=True)
class TaskStatus:
    task_id: str
    phase: str
    stop_reason: StopReason | None
    iterations: int
    tools_used: int


@dataclass(frozen=True)
class BotSpec:
    name: str
    soul: str
    agents: str
    model: str | None
    tools: list[str] | None

    @property
    def system_prompt(self) -> str:
        """SOUL 在前（身份先于规则），分隔符拼接（唯一权威在 events-bots.md）。"""
        return f"{self.soul}\n\n---\n\n{self.agents}"


@dataclass
class RuntimeConfig:
    db_path: str | Path = Path.home() / ".democlaw" / "democlaw.db"
    bots_dir: str | Path = Path("bots")
    workspace_root: str | Path = Path("workspace")
    model: str = "openai/gpt-4o"
    api_key_env: str = "OPENAI_API_KEY"
    max_iterations: int = 50
    context_window: int = 128000
    stream_fn: Any | None = None     # 注入 mock 模型（测试确定性）
    tools: dict[str, Tool] | None = None  # 注入自定义工具


# ── 组合根 ──


class BotRuntime:
    @classmethod
    def build(cls, config: RuntimeConfig | None = None) -> BotRuntime:
        return cls(config or RuntimeConfig())

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self._storage = Storage(str(config.db_path))
        self._session_mgr = SessionManager(self._storage)
        self._bus = EventBus()

        # 工具注册表：内置扩展工具 + 注入工具
        self._registry = ToolRegistry()
        from .tools._builtin import register_builtin_tools  # 第 3 步扩展
        register_builtin_tools(self._registry)
        if config.tools:
            for t in config.tools.values():
                self._registry.register(t)

        # bot spec 装配（三文件）
        self._bots: dict[str, BotSpec] = self._load_bots(config.bots_dir)

        # 启动恢复扫描（进程重启后物化中断）
        scan_and_recover(self._storage, self._session_mgr)

    # ── 同步/async 双形态 ──

    def _sync(self, coro) -> Any:
        """同步形态：在**当前线程**用 asyncio.run 跑协程并阻塞等结果。

        不用独立循环线程——SQLite 连接在哪个线程创建就得在哪个线程用
        （single-process asyncio + sqlite3 直接在事件循环线程执行，见
        architecture.md 附录）；独立线程会违反 SQLite 的 check_same_thread。
        因此同步形态禁止在 async 语境调用（asyncio.run 不可嵌套）。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # 无运行中循环，可同步
        else:
            raise RuntimeError(
                "当前已在 async 语境，禁止调用同步 run()/result()；请改用 arun()/aresult()"
            )
        return asyncio.run(coro)

    def close(self) -> None:
        self._storage.close()

    # ── bot 装配 ──

    def _load_bots(self, bots_dir: str | Path) -> dict[str, BotSpec]:
        root = Path(bots_dir)
        if not root.is_absolute():
            root = Path.cwd() / root
        specs: dict[str, BotSpec] = {}
        if not root.exists():
            return specs
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            name = sub.name
            soul = (sub / "SOUL.md").read_text(encoding="utf-8")
            agents = (sub / "AGENTS.md").read_text(encoding="utf-8")
            spec_json = sub / "spec.json"
            model = None
            tools = None
            if spec_json.exists():
                data = json.loads(spec_json.read_text(encoding="utf-8"))
                model = data.get("model")
                tools = data.get("tools")
            specs[name] = BotSpec(name=name, soul=soul, agents=agents, model=model, tools=tools)
        return specs

    def bots(self) -> list[str]:
        return list(self._bots.keys())

    def bot(self, name: str) -> Bot:
        if name not in self._bots:
            raise KeyError(f"bot 不存在：{name}（可用：{', '.join(self.bots())}）")
        return Bot(self, self._bots[name])

    # ── 会话树（门面动词） ──

    def new_session(self, bot_id: str) -> SessionInfo:
        def _do():
            return self._session_mgr.submit("", NewOp("", bot_id=bot_id))
        return self._sync(_do())

    async def anew_session(self, bot_id: str) -> SessionInfo:
        return await self._session_mgr.submit("", NewOp("", bot_id=bot_id))

    def sessions(self) -> list[SessionInfo]:
        return [self._row_to_info(r) for r in self._session_mgr._storage.sessions_all()]

    def fork_session(self, session_id: str, at_entry_id: str) -> SessionInfo:
        return self._sync(self._session_mgr.submit(session_id, ForkOp(session_id, at_entry_id)))

    def reset_session(self, session_id: str, to_entry_id: str) -> None:
        self._sync(self._session_mgr.submit(session_id, ResetOp(session_id, to_entry_id)))

    def delete_session(self, session_id: str) -> None:
        self._sync(self._session_mgr.submit(session_id, DeleteOp(session_id)))

    def history(self, session_id: str) -> list[EntryRow]:
        return self._sync(self._session_mgr.submit(session_id, HistoryOp(session_id)))

    def sessions_with(self, stop_reason: StopReason) -> list[SessionInfo]:
        """枚举含指定终态任务的会话——重启后发现 interrupted 任务的入口。"""
        out: list[SessionInfo] = []
        from .core.session import replay_entries
        for srow in self._storage.sessions_all():
            if srow.active_leaf is None:
                continue
            prefix = replay_entries(self._storage.entries(srow.id), srow.active_leaf)
            for e in prefix:
                if e.kind == "interrupted" and stop_reason == StopReason.INTERRUPTED:
                    out.append(self._row_to_info(srow))
                    break
                if is_terminal_entry(e) and stop_reason_of(e) == stop_reason:
                    out.append(self._row_to_info(srow))
                    break
        return out

    @staticmethod
    def _row_to_info(srow: SessionRow) -> SessionInfo:
        return SessionInfo(
            id=srow.id,
            bot_id=srow.bot_id,
            parent_id=srow.parent_id,
            active_entry_id=srow.active_leaf,
            running_task_id=None,
            model=srow.model,
            title=srow.title,
            created_at=srow.created_at,
            background=bool(srow.background),
            restart_count=srow.restart_count,
        )


# ── Bot：无状态视图 ──


class Bot:
    def __init__(self, runtime: BotRuntime, spec: BotSpec):
        self._runtime = runtime
        self._spec = spec

    @property
    def name(self) -> str:
        return self._spec.name

    def run(self, task: str, *, session_id: str | None = None,
            model: str | None = None, max_iterations: int | None = None) -> TaskResult:
        return self._runtime._sync(self._run(task, session_id, model, max_iterations))

    async def arun(self, task: str, *, session_id: str | None = None,
                   model: str | None = None, max_iterations: int | None = None) -> TaskResult:
        return await self._run(task, session_id, model, max_iterations)

    async def _run(self, task: str, session_id: str | None,
                   model: str | None, max_iterations: int | None) -> TaskResult:
        rt = self._runtime
        sid = session_id
        if sid is None:
            info = await rt._session_mgr.submit("", NewOp("", bot_id=self.name))
            sid = info.id
        # 白名单校验 + 任务 id
        task_id = f"task_{int(time.time() * 1000)}"
        # 输入 append + mark_running
        await rt._session_mgr.submit(sid, AppendOp(sid, "user", task, parent=None, task_id=task_id))
        rt._session_mgr.mark_running(sid, task_id)

        loop_runtime = self._make_loop_runtime(model, max_iterations)
        result = await run_task_loop(sid, task_id, task, loop_runtime)
        return TaskResult(
            stop_reason=result.stop_reason,
            content=result.content,
            error=result.error,
            task_id=task_id,
            session_id=sid,
            iterations=result.iterations,
            tools_used=result.tools_used,
        )

    def _make_loop_runtime(self, model: str | None, max_iterations: int | None) -> LoopRuntime:
        rt = self._runtime
        stream_fn = rt.config.stream_fn
        if stream_fn is None:
            import os

            from .providers.openai import make_openai_stream_fn
            key = os.environ.get(rt.config.api_key_env)
            if not key:
                raise RuntimeError(
                    f"缺少 API key：环境变量 {rt.config.api_key_env} 未设置。"
                    f"教学示例可用 RuntimeConfig(stream_fn=mock) 注入 mock 模型，"
                    f"或 export {rt.config.api_key_env}=sk-..."
                )
            stream_fn = make_openai_stream_fn(key)
        return LoopRuntime(
            session_mgr=rt._session_mgr,
            stream_fn=stream_fn,
            registry=rt._registry,
            event_bus=rt._bus,
            system_prompt=self._spec.system_prompt,
            model=model or self._spec.model or rt.config.model,
            max_iterations=max_iterations or rt.config.max_iterations,
            context_window=rt.config.context_window,
            finish_turn=lambda ctx: False,
        )