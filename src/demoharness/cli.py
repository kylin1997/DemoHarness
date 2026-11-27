"""Launch the local DemoHarness web interface."""

import argparse
from pathlib import Path

from aiohttp import web

from .application import Application


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="demoharness", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动本机三栏界面")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--state-dir", type=Path, default=Path.home() / ".demoharness-v2")
    serve.add_argument("--workspace", type=Path)
    serve.add_argument("--skills", type=Path, default=Path("skills"))
    serve.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 至 65535 之间")
    try:
        application = Application(args.state_dir, args.workspace, args.skills, args.config)
        web.run_app(application.make_app(), host="127.0.0.1", port=args.port, handler_cancellation=True)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"无法启动 DemoHarness：{exc}\n")
    return 0
