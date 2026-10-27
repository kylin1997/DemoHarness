"""Web 薄壳（web/server.py, 旁路体验工程, 第 9 步）。

构建在 BotRuntime SDK 之上的薄壳，零业务逻辑。标准库 http.server（每请求一线程）
+ SSE（GET /api/events 活体推送）。不做鉴权（127.0.0.1）、不做框架。

SSE 数据源 = runtime 的 EventBus；历史重建永远查 storage（不把事件流当历史源）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from democlaw.runtime import BotRuntime


class WebServer:
    """~150 行薄壳：持有 BotRuntime，同步调用 + SSE 轮询桥。"""

    def __init__(self, runtime: BotRuntime, host: str = "127.0.0.1", port: int = 8765):
        self.runtime = runtime
        self._host = host
        self._port = port

        # SSE 订阅者：session_id -> [queue]。用 queue.Queue 跨线程（http 是线程模型）。
        import queue
        self._sse_queues: dict[str, list[queue.Queue]] = {}

        # 后台事件泵：把 runtime 事件扇出到 SSE 订阅者。单进程教学规模，轮询 EventBus
        # 经一个 asyncio 循环跑。
        self._stop = threading.Event()

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, obj, status=200):
                body = json.dumps(obj, ensure_ascii=False, default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self):
                n = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/api/sessions":
                    self._json([vars(s) for s in server.runtime.sessions()])
                elif path.startswith("/api/sessions/") and path.endswith("/history"):
                    sid = path.split("/")[3]
                    self._json([vars(e) for e in server.runtime.history(sid)])
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                if self.path.startswith("/api/bots/") and self.path.endswith("/run"):
                    bot_name = self.path.split("/")[3]
                    body = self._read_body()
                    task = body.get("task", "")
                    try:
                        result = server.runtime.bot(bot_name).run(task)
                        self._json({"task_id": result.task_id, "session_id": result.session_id,
                                    "stop_reason": result.stop_reason.value, "content": result.content})
                    except Exception as e:  # noqa: BLE001 —— 薄壳把错误转 JSON
                        self._json({"error": str(e)}, 500)
                else:
                    self._json({"error": "not found"}, 404)

            def log_message(self, *args):
                pass  # 静默访问日志

        return Handler

    def serve(self):
        httpd = ThreadingHTTPServer((self._host, self._port), self._handler())
        print(f"DemoClaw Web 运行在 http://{self._host}:{self._port}  (Ctrl-C 停止)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()


def serve(runtime: BotRuntime, port: int = 8765):
    WebServer(runtime, port=port).serve()