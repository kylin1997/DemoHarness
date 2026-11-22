"""The local console for the Agent runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from dataclasses import asdict
from pathlib import Path

from .providers.openai import make_openai_stream_fn
from .runtime import AgentDefinition, AgentRuntime


def _definitions(args) -> list[AgentDefinition]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("请设置 OPENAI_API_KEY，或先运行 README 的无凭据示例。")
    complete = make_openai_stream_fn(key, os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    model = os.environ.get("MODEL", "gpt-4o")
    if args.config:
        data = json.loads(args.config.read_text(encoding="utf-8"))
        configs = data["agents"]
        config_root = args.config.resolve().parent
    else:
        configs = [{"id": args.agent, "project": str(args.project.resolve())}]
        config_root = args.state_dir.resolve() / "agents"
    definitions = []
    for config in configs:
        agent_id = config["id"]
        if not isinstance(agent_id, str) or not agent_id or any(c not in
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in agent_id):
            raise ValueError("Agent ID 仅允许字母、数字、连字符和下划线")
        root = config_root / config.get("root", agent_id)
        prompt = "\n\n".join((root / name).read_text(encoding="utf-8")
                             for name in ("SOUL.md", "AGENTS.md") if (root / name).is_file())
        definitions.append(AgentDefinition(
            agent_id, config_root / config["project"], config.get("model", model), complete,
            system_prompt=prompt, proactive_writes=config.get("proactive_writes", False),
        ))
    return definitions


def _show(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str), flush=True)


async def _lines():
    if stat.S_ISREG(os.fstat(sys.stdin.fileno()).st_mode):
        for line in sys.stdin:
            yield line.strip()
        return
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    try:
        async for line in reader:
            yield line.decode("utf-8").strip()
    finally:
        transport.close()


async def _print_notices(agent):
    async for notice in agent.updates():
        if notice.task_id:
            print(f"[{agent.id}/{notice.task_id}] {notice.text}", flush=True)
        else:
            print(f"[{agent.id}] {notice.text}", flush=True)


async def _chat(args):
    definitions = _definitions(args)
    async with AgentRuntime.open(args.state_dir, definitions) as runtime:
        agent = runtime.agent(args.agent)
        printers = [asyncio.create_task(_print_notices(runtime.agent(d.id))) for d in definitions]
        try:
            print(f"Agent {agent.id} 已启动。输入消息，或用 /help 查看命令。", flush=True)
            async for line in _lines():
                if not line:
                    continue
                command, _, value = line.partition(" ")
                try:
                    if command == "/exit":
                        break
                    if command == "/help":
                        print("/agent ID · /tasks · /show TASK · /history TASK · /tell TASK MESSAGE · "
                              "/stop TASK · /resume TASK · /memory · /remember TEXT · "
                              "/correct ID VERSION TEXT · /forget ID · /exit", flush=True)
                    elif command == "/agent":
                        agent = runtime.agent(value)
                    elif command == "/tasks":
                        _show([{"id": t.id, "goal": t.goal, "state": t.state} for t in agent.tasks()])
                    elif command == "/show":
                        _show(asdict(agent.task(value)))
                    elif command == "/history":
                        _show([asdict(entry) for entry in agent.history(value)])
                    elif command == "/tell":
                        task_id, _, message = value.partition(" ")
                        await agent.send(message, task_id=task_id)
                    elif command == "/stop":
                        await agent.stop(value)
                    elif command == "/resume":
                        await agent.resume(value)
                    elif command == "/memory":
                        _show([asdict(memory) for memory in agent.memories()])
                    elif command == "/remember":
                        _show([asdict(memory) for memory in agent.remember(value)])
                    elif command == "/correct":
                        memory_id, version, content = value.split(maxsplit=2)
                        _show(asdict(agent.correct_memory(int(memory_id), int(version), content)))
                    elif command == "/forget":
                        _show({"deleted": agent.delete_memory(int(value))})
                    elif command.startswith("/"):
                        print("未知命令。输入 /help 查看用法。", flush=True)
                    else:
                        await agent.send(line)
                except (ValueError, KeyError) as exc:
                    print(f"无法处理：{exc}", flush=True)
        finally:
            for printer in printers:
                printer.cancel()
            await asyncio.gather(*printers, return_exceptions=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="demoharness", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    chat = commands.add_parser("chat", help="在本机保持 Agent 运行并接收消息")
    chat.add_argument("agent")
    chat.add_argument("--project", type=Path, default=Path.cwd())
    chat.add_argument("--state-dir", type=Path, default=Path.home() / ".demoharness")
    chat.add_argument("--config", type=Path, help="指定包含 agents 列表的 JSON 配置")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_chat(args))
    except (KeyError, ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    except KeyboardInterrupt:
        return 130
    return 0
