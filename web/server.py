"""Web 薄壳（web/server.py, 旁路体验工程, 第 9 步）。

构建在 BotRuntime SDK 之上的薄壳，零业务逻辑。标准库 http.server（每请求一线程）
+ SSE（GET /api/events 活体推送）。不做鉴权（127.0.0.1）、不做框架。

实时模型：POST /run 用 `bot.start()` 把任务丢进后台 asyncio 循环，立即返回
{task_id, session_id}；SSE 从 runtime 的 EventBus 订阅事件逐帧推给浏览器。
历史重建永远查 storage（不把事件流当历史源）。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from democlaw.runtime import BotRuntime, RuntimeConfig


class WebServer:
    """薄壳：持有 BotRuntime；一个后台 asyncio 循环跑任务；SSE 活体推事件。"""

    def __init__(self, runtime: BotRuntime, host: str = "127.0.0.1", port: int = 8765):
        self.runtime = runtime
        self._host = host
        self._port = port

        # 后台 asyncio 循环（跨线程跑 start()/submit）
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

        # SSE 订阅者：全局一份 EventSource 订阅，前端按 session_id 过滤。存 (queue, last_seq) 用于逐帧推送。
        self._sse_sub = None

    def _run_coro(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _spawn(self, coro):
        """在后台 loop 上 fire-and-forget 一个协程（不等待结果）。"""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _start_task(self, bot_name: str, task: str):
        bot = self.runtime.bot(bot_name)
        handle = bot.start(task)  # start 在后台 loop（async 语境）内建 loop task
        return handle

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

            def _serve_static(self, path):
                if path in ("/", "/index.html"):
                    path = "/index.html"
                try:
                    with open(f"web/static{path}", "rb") as fh:
                        body = fh.read()
                except OSError:
                    self._json({"error": "not found"}, 404)
                    return
                ctype = "text/html" if path.endswith(".html") else "application/javascript" if path.endswith(".js") else "text/css"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/index.html") or path.endswith((".js", ".css")):
                    self._serve_static(path)
                elif path == "/api/sessions":
                    self._json([vars(s) for s in server.runtime.sessions()])
                elif path.startswith("/api/sessions/") and path.endswith("/history"):
                    sid = path.split("/")[3]
                    self._json([vars(e) for e in server.runtime.history(sid)])
                elif path == "/api/bots":
                    self._json(server.runtime.bots())
                elif path == "/api/events":
                    self._serve_sse()
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                if self.path.startswith("/api/bots/") and self.path.endswith("/run"):
                    bot_name = self.path.split("/")[3]
                    body = self._read_body()
                    task = body.get("task", "")
                    try:
                        handle = server._run_coro(server._start_task(bot_name, task))
                        # start() 是同步的（内部 async 建 loop task），但 bot.start 需要 async 语境。
                        self._json({"task_id": handle.task_id, "session_id": handle._session_id})
                    except Exception as e:  # noqa: BLE001
                        self._json({"error": str(e)}, 500)
                else:
                    self._json({"error": "not found"}, 404)

            def _serve_sse(self):
                """SSE：订阅 EventBus，把事件逐帧推给浏览器。全局一条连接，前端按 session 过滤。"""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    # 跨线程拿事件：在后台 loop 上订阅 runtime._bus，events 经队列桥回本线程。
                    # _bridge_events 是无限协程，fire-and-forget 投到后台 loop，不阻塞本线程。
                    import queue
                    q = queue.Queue()
                    server._spawn(server._bridge_events(q))
                    while True:
                        try:
                            item = q.get(timeout=30)
                        except queue.Empty:
                            # 心跳防断
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        if item is None:
                            break
                        self.wfile.write(item.encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        return Handler

    async def _bridge_events(self, q):
        """在后台 loop 上订阅 EventBus，把事件转成 SSE chunk 塞进跨线程队列。"""
        sub = self.runtime._bus.subscribe()
        while True:
            ev = await sub.__anext__()
            data = json.dumps({"task_id": ev.task_id, "session_id": ev.session_id, "type": ev.type, "data": ev.data}, ensure_ascii=False)
            q.put(f"data: {data}\n\n")

    def serve(self):
        httpd = ThreadingHTTPServer((self._host, self._port), self._handler())
        print(f"DemoClaw Web 运行在 http://{self._host}:{self._port}  (Ctrl-C 停止)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()


def build_runtime(db_path: str, bots_dir: str = "bots", stream_fn=None) -> BotRuntime:
    return BotRuntime.build(RuntimeConfig(db_path=db_path, bots_dir=bots_dir, stream_fn=stream_fn))


def serve(runtime: BotRuntime, port: int = 8765):
    WebServer(runtime, port=port).serve()