"""A local stdio MCP server for the guide's discovery exercise."""

import json
import sys


def handle(request):
    match request["method"]:
        case "initialize":
            return {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demoharness-echo", "version": "1"},
            }
        case "tools/list":
            return {"tools": [{
                "name": "echo", "description": "Echo or 回显 a line of text",
                "inputSchema": {
                    "type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"], "additionalProperties": False,
                },
            }]}
        case "tools/call":
            params = request["params"]
            if params["name"] != "echo":
                raise ValueError("Unknown tool")
            return {"content": [{"type": "text", "text": params["arguments"]["text"]}]}
        case _:
            raise ValueError("Method not found")


for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    response = {"jsonrpc": "2.0", "id": request["id"]}
    try:
        response["result"] = handle(request)
    except (ValueError, KeyError):
        response["error"] = {"code": -32602, "message": "Unsupported method or parameters"}
    print(json.dumps(response, ensure_ascii=False), flush=True)
