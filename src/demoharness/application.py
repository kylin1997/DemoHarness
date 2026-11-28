"""Assemble DemoHarness and expose the local HTTP and SSE interface."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from aiohttp import web

from .guardrails import Guardrails
from .harness import DemoHarness
from .models import OpenAiChatModel, ScriptedChatModel
from .skills import Skills
from .state import DEFAULT_OPTIONS, Store
from .toolbox import ToolRegistry

REASONING = ("default", "low", "medium", "high")
MODES = ("react", "plan")
ASSETS = Path(__file__).with_name("web")
LOCAL_HOST = re.compile(r"(?:127\.0\.0\.1|localhost)(?::[0-9]+)?\Z")


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


@web.middleware
async def local_boundary(request: web.Request, handler):
    if not LOCAL_HOST.fullmatch(request.host):
        return _error("DemoHarness 仅接受本机地址。", 403)
    origin = request.headers.get("Origin")
    if origin and origin != f"{request.scheme}://{request.host}":
        return _error("请求来源与当前界面不一致。", 403)
    try:
        return await handler(request)
    except (TypeError, ValueError) as exc:
        return _error(str(exc))
    except KeyError:
        return _error("找不到该会话。", 404)


async def _body(request: web.Request) -> dict:
    if request.content_type != "application/json":
        raise ValueError("请求体必须是 JSON 对象。")
    data = await request.json()
    if not isinstance(data, dict):
        raise TypeError("请求体必须是 JSON 对象。")
    return data


class Application:
    def __init__(
        self,
        state_dir: Path,
        workspace: Path | None,
        skills_dir: Path,
        config_path: Path | None = None,
    ):
        self.state_dir = state_dir.expanduser().resolve()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.workspace = (
            (workspace or self.state_dir / "workspace").expanduser().resolve()
        )
        self.workspace.mkdir(parents=True, exist_ok=True)
        if workspace is None:
            demo_file = self.workspace / "notes.md"
            try:
                with demo_file.open("x", encoding="utf-8") as notes:
                    notes.write(
                        "# DemoHarness 练习\n\n一次真实执行比一句完成声明更有说服力。\n"
                    )
            except FileExistsError:
                pass

        location = config_path or self.state_dir / "config.json"
        config = (
            json.loads(location.read_text(encoding="utf-8"))
            if config_path or location.is_file()
            else {}
        )
        if not isinstance(config, dict):
            raise TypeError("配置文件必须是 JSON 对象。")
        self.mcp_config = config.get("mcp", [])
        if not isinstance(self.mcp_config, list):
            raise TypeError("mcp 配置必须是列表。")
        self.models = {"demo": ScriptedChatModel()}
        self.profiles = [{"id": "demo", "label": "离线演示", "model": "scripted"}]
        if os.environ.get("OPENAI_API_KEY"):
            self._add_profile(
                {
                    "id": "openai",
                    "label": "OpenAI 兼容",
                    "model": os.getenv("MODEL", "gpt-4o-mini"),
                    "base_url": os.getenv(
                        "OPENAI_BASE_URL", "https://api.openai.com/v1"
                    ),
                    "api_key_env": "OPENAI_API_KEY",
                }
            )
        profiles = config.get("profiles", [])
        if not isinstance(profiles, list):
            raise TypeError("profiles 配置必须是列表。")
        for profile in profiles:
            self._add_profile(profile)

        self.registry = ToolRegistry()
        self.skills = Skills(skills_dir.expanduser().resolve())
        self.store = Store(self.state_dir / "sessions")
        self.guardrails = Guardrails(self.store)
        self.harness = DemoHarness(
            self.store,
            self.registry,
            self.guardrails,
            self.skills,
            self.models,
            self.workspace,
        )
        self.active = set()

    def _add_profile(self, profile: dict) -> None:
        if not isinstance(profile, dict):
            raise TypeError("模型档案必须是 JSON 对象。")
        profile_id = profile.get("id", "")
        model = profile.get("model", "")
        base_url = profile.get("base_url", "")
        key_name = profile.get("api_key_env", "OPENAI_API_KEY")
        label = profile.get("label", profile_id)
        if not all(
            isinstance(value, str) and value.strip()
            for value in (profile_id, model, base_url, key_name, label)
        ):
            raise ValueError("模型档案需要有效的 id、model、base_url 和 api_key_env。")
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", profile_id)
            or profile_id in self.models
        ):
            raise ValueError(f"模型档案 ID 无效或重复：{profile_id}")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("模型档案的 base_url 必须使用 HTTP 或 HTTPS。")
        api_key = os.getenv(key_name)
        if not api_key:
            raise ValueError(f"模型档案 {profile_id} 需要环境变量 {key_name}。")
        self.models[profile_id] = OpenAiChatModel(base_url, api_key, model)
        self.profiles.append({"id": profile_id, "label": label, "model": model})

    def _options(self, options: dict) -> dict:
        if not isinstance(options, dict) or set(options) - set(DEFAULT_OPTIONS):
            raise ValueError("配置只接受 profile、reasoning 和 mode。")
        allowed = {"profile": tuple(self.models), "reasoning": REASONING, "mode": MODES}
        for field, value in options.items():
            if not isinstance(value, str) or value not in allowed[field]:
                raise ValueError(f"无效的配置选项：{field}")
        return options

    def make_app(self) -> web.Application:
        app = web.Application(middlewares=[local_boundary], client_max_size=256 * 1024)
        app.cleanup_ctx.append(self._lifecycle)
        app.router.add_get("/", self.index)
        app.router.add_static("/assets", ASSETS)
        app.router.add_get("/api/config", self.configuration)
        app.router.add_get("/api/sessions", self.list_sessions)
        app.router.add_post("/api/sessions", self.create_session)
        app.router.add_get("/api/sessions/{session_id}", self.session_detail)
        app.router.add_patch("/api/sessions/{session_id}", self.update_session)
        app.router.add_post("/api/chat", self.chat)
        app.router.add_post("/api/approve", self.approve)
        return app

    async def _lifecycle(self, _app):
        try:
            await self.registry.connect_mcp(self.mcp_config)
            yield
        finally:
            try:
                await self.registry.close()
            finally:
                self.store.close()

    async def index(self, _request):
        return web.FileResponse(
            ASSETS / "index.html", headers={"Cache-Control": "no-store"}
        )

    async def configuration(self, _request):
        return web.json_response(
            {
                "profiles": self.profiles,
                "reasoning": REASONING,
                "modes": MODES,
                "defaults": DEFAULT_OPTIONS,
            }
        )

    async def list_sessions(self, _request):
        return web.json_response({"sessions": self.store.list_sessions()})

    async def create_session(self, request):
        data = await _body(request) if request.can_read_body else {}
        title = data.get("title", "新会话")
        if not isinstance(title, str) or not title.strip() or len(title) > 80:
            raise ValueError("会话名称需要 1 至 80 个字符。")
        session = self.store.create_session(title.strip())
        return web.json_response(session, status=201)

    async def session_detail(self, request):
        session_id = request.match_info["session_id"]
        return web.json_response(
            {
                "session": self.store.session(session_id),
                "messages": self.store.messages(session_id),
                "events": self.store.events(session_id),
            }
        )

    async def update_session(self, request):
        data = await _body(request)
        if "options" not in data:
            raise ValueError("缺少会话配置。")
        session = self.store.update_options(
            request.match_info["session_id"], self._options(data["options"])
        )
        return web.json_response(session)

    async def chat(self, request):
        data = await _body(request)
        session_id, message = data.get("session_id"), data.get("message")
        if not isinstance(session_id, str) or not isinstance(message, str):
            raise TypeError("需要会话 ID 和文本消息。")
        session = self.store.session(session_id)
        if session_id in self.active:
            return _error("当前会话正在执行，等本轮结束后再发送。", 409)
        settings = {**DEFAULT_OPTIONS, **session["options"]}
        settings.update(
            {field: data[field] for field in DEFAULT_OPTIONS if field in data}
        )
        self._options(settings)
        self.store.update_options(session_id, settings)
        if session["title"] == "新会话" and message.strip():
            self.store.rename(session_id, " ".join(message.split())[:32])

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )

        async def emit(event: dict) -> None:
            payload = json.dumps(event, ensure_ascii=False)
            await response.write(
                f"event: {event['type']}\ndata: {payload}\n\n".encode()
            )

        self.active.add(session_id)
        try:
            await response.prepare(request)
            await self.harness.chat(session_id, message, settings, emit)
            await response.write_eof()
        except ConnectionResetError:
            pass
        finally:
            self.active.discard(session_id)
        return response

    async def approve(self, request):
        data = await _body(request)
        session_id, approval_id, decision = (
            data.get(key) for key in ("session_id", "approval_id", "decision")
        )
        if (
            not isinstance(session_id, str)
            or not isinstance(approval_id, str)
            or decision not in ("allow", "trust", "deny")
        ):
            raise ValueError("需要会话 ID、审批 ID 和 allow、trust 或 deny 决策。")
        if not self.guardrails.resolve(session_id, approval_id, decision):
            return _error("审批已结束或不属于当前会话。", 409)
        return web.json_response({"accepted": True})
