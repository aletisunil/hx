#!/usr/bin/env python3
"""A minimal MCP server over stdio, for tests.

Real subprocess, real JSON-RPC framing. Mocking the transport would not have
caught the things that actually go wrong here - initialize handshakes, tool
namespacing, or a server that dies mid-session.

Behaviour is driven by argv:
  (none)        healthy server with two tools
  --no-tools    advertises no tools capability
  --crash       exits immediately after start
  --hang        accepts the connection and never replies
  --noise       prints non-JSON to stdout before responding
"""

from __future__ import annotations

import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else ""

TOOLS = [
    {
        "name": "echo",
        "description": "Echo a message back",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle(message: dict) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        capabilities = {} if MODE == "--no-tools" else {"tools": {}}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": capabilities,
                "serverInfo": {"name": "echo-server", "version": "1.0"},
            },
        }

    if method == "tools/list":
        # Deliberately unsorted: the manager must impose a stable order itself.
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": list(reversed(TOOLS))}}

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}

        if name == "echo":
            text = str(arguments.get("message", ""))
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        if name == "add":
            total = float(arguments.get("a", 0)) + float(arguments.get("b", 0))
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": str(total)}]},
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": f"no such tool {name}"}],
                "isError": True,
            },
        }

    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": "method not found"},
    }


def main() -> None:
    if MODE == "--crash":
        sys.exit(1)
    if MODE == "--noise":
        print("starting up, definitely not JSON")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if MODE == "--hang":
            time.sleep(60)
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(message)
        if response is not None:
            send(response)


if __name__ == "__main__":
    main()
