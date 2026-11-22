"""A small stdio MCP server for the MCP tests.

Usage: python fake_mcp_server.py [--exit-at-start | --no-reply | --tool-names a,b,...]

Without options it serves the tools in TOOLS, two per `tools/list` page.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

READ_ONLY = {"readOnlyHint": True}
TOOLS = [
    {"name": "echo", "description": "Return the text.", "annotations": READ_ONLY,
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "delete_note", "description": "Delete a note.", "annotations": {"destructiveHint": True}},
    {"name": "status", "description": "Service status.",
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
    {"name": "fetch", "description": "Fetch a page."},
    {"name": "structured", "description": "Return structured content.", "annotations": READ_ONLY},
    {"name": "fail", "description": "Return an error result.", "annotations": READ_ONLY},
    {"name": "sleep", "description": "Sleep for `seconds`.", "annotations": READ_ONLY},
    {"name": "environment", "description": "Return the environment and working directory.", "annotations": READ_ONLY},
    {"name": "ask_client", "description": "Send requests to the client.", "annotations": READ_ONLY},
    {"name": "start_child", "description": "Start a child process that sleeps.", "annotations": READ_ONLY},
    {"name": "exit", "description": "Exit the server.", "annotations": READ_ONLY},
]
PAGE_SIZE = 2


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def text(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}]}


def ask_client() -> dict:
    send({"jsonrpc": "2.0", "id": "s1", "method": "ping"})
    send({"jsonrpc": "2.0", "id": "s2", "method": "sampling/createMessage", "params": {}})
    answers = {}
    while len(answers) < 2:
        message = json.loads(sys.stdin.readline())
        answers[message["id"]] = message
    return {"content": [], "structuredContent": answers}


def call(name: str, arguments: dict) -> dict:
    if name == "echo":
        return text(arguments.get("text", ""))
    if name == "structured":
        return {"content": [{"type": "text", "text": "ignored"}], "structuredContent": {"count": 3}}
    if name == "fail":
        return {"content": [{"type": "text", "text": "the note does not exist"}], "isError": True}
    if name == "sleep":
        time.sleep(arguments.get("seconds", 10))
        return text("slept")
    if name == "environment":
        return {"content": [], "structuredContent": {"env": dict(os.environ), "cwd": os.getcwd()}}
    if name == "ask_client":
        return ask_client()
    if name == "start_child":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        return text(str(child.pid))
    if name == "exit":
        sys.exit(3)
    return {"content": [{"type": "text", "text": f"{name} called"}, {"type": "image", "data": "", "mimeType": "image/png"}]}


def main() -> None:
    args = sys.argv[1:]
    if "--exit-at-start" in args:
        print("cannot start: missing database", file=sys.stderr, flush=True)
        sys.exit(1)
    tools = TOOLS
    if "--tool-names" in args:
        tools = [{"name": name} for name in args[args.index("--tool-names") + 1].split(",")]
    print("fake server started", file=sys.stderr, flush=True)
    for line in sys.stdin:
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if request_id is None or "--no-reply" in args:
            continue
        params = message.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake", "version": "1"}}
        elif method == "tools/list":
            start = int(params.get("cursor", "0"))
            result = {"tools": tools[start:start + PAGE_SIZE]}
            if start + PAGE_SIZE < len(tools):
                result["nextCursor"] = str(start + PAGE_SIZE)
        elif method == "tools/call":
            result = call(params["name"], params.get("arguments") or {})
        elif method == "ping":
            result = {}
        else:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}})
            continue
        send({"jsonrpc": "2.0", "id": request_id, "result": result})


if __name__ == "__main__":
    main()
