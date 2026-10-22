"""A stand-in for the ``claude`` CLI, speaking its stream-json protocol for real.

Installed on the e2e ``PATH`` as ``claude``, so everything HX does with Claude
Code - the argv and environment it starts the CLI with, the control protocol,
the MCP server it serves over it, held tool calls, the session files it
rebuilds - runs as shipped. Only the model is scripted.

State lives in ``$E2E_CLAUDE_DIR``:

* ``script.json`` - the model turns, one queue shared by every process the
  test starts; ``cursor`` says how far it has been read;
* ``auth.json`` - what ``claude auth status`` reports (absent: signed out);
* ``status_delay`` - seconds ``claude auth status`` takes to answer;
* ``login_error`` - why ``claude auth login`` fails, whatever code is pasted;
* ``version`` and ``models.json`` - which Claude Code this is: what
  ``--version`` prints and the models ``initialize`` lists (absent: 2.1.205
  and :data:`MODELS`);
* ``log.jsonl`` - one line per thing a process saw or did, for assertions.

HX's own bookkeeping calls - naming a session, summarising one for a compaction
- are one-offs recognised by their system prompts and answered without the
script, as the OpenRouter stub answers the naming call.
"""

from __future__ import annotations

import fcntl
import json
import os
import queue
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

STATE = Path(os.environ["E2E_CLAUDE_DIR"])
LOG = STATE / "log.jsonl"
TITLE_SYSTEM = "You name things briefly and precisely."
SUMMARY_SYSTEM = "You write precise handover summaries."
PID = os.getpid()

MODELS = [
    {
        "value": "default",
        "resolvedModel": "claude-sonnet-5",
        "displayName": "Default (recommended)",
        "description": "Sonnet 5 · Efficient for routine tasks",
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
    },
    {
        "value": "sonnet",
        "resolvedModel": "claude-sonnet-5",
        "displayName": "Sonnet",
        "description": "Sonnet 5 · Efficient for routine tasks",
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
    },
    {
        "value": "opus",
        "resolvedModel": "claude-opus-4-8",
        "displayName": "Opus",
        "description": "Opus 4.8 · Best for everyday, complex tasks · ~2× usage vs Sonnet",
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
    },
    {
        "value": "haiku",
        "resolvedModel": "claude-haiku-4-5-20251001",
        "displayName": "Haiku",
        "description": "Haiku 4.5 · Fastest for quick answers",
    },
]


def models() -> list[dict[str, Any]]:
    path = STATE / "models.json"
    return json.loads(path.read_text()) if path.exists() else MODELS


DEFAULT_LIMITS = (200_000, 64_000)
"""``(contextWindow, maxOutputTokens)`` for a model with nothing set for it."""


def limits() -> dict[str, tuple[int, int]]:
    """Per resolved model, the limits a turn reports - set by ``FakeClaude.limits``."""
    path = STATE / "limits.json"
    if not path.exists():
        return {}
    return {model: (pair[0], pair[1]) for model, pair in json.loads(path.read_text()).items()}


def version() -> str:
    path = STATE / "version"
    return path.read_text().strip() if path.exists() else "2.1.205"


def log(kind: str, **fields: Any) -> None:
    line = json.dumps({"pid": PID, "kind": kind, "t": time.time(), **fields})
    with LOG.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line + "\n")


def emit(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


# -- `claude auth ...` -------------------------------------------------------------


def auth(args: list[str]) -> int:
    path = STATE / "auth.json"
    if args[:1] == ["status"]:
        log("status", env={k: v for k, v in os.environ.items() if _interesting(k)})
        delay = STATE / "status_delay"
        if delay.exists():
            # A slow answer: a keychain prompt, a cold Node start, a busy disk.
            time.sleep(float(delay.read_text()))
        print(
            path.read_text()
            if path.exists()
            else json.dumps({"loggedIn": False, "authMethod": "none"})
        )
        return 0
    if args[:1] == ["login"]:
        log("login", argv=args)
        print("Opening browser to sign in…")
        print("If the browser didn't open, visit: https://claude.example/oauth/authorize?code=true")
        sys.stdout.write("Paste code here if prompted > ")
        sys.stdout.flush()
        code = sys.stdin.readline().strip()
        failure = STATE / "login_error"
        if failure.exists() or code != "good-code":
            # On the prompt's line, as Claude Code 2.1 prints it when stdin is a pipe.
            reason = (
                failure.read_text() if failure.exists() else "Request failed with status code 400"
            )
            print(f"Login failed: {reason}")
            return 1
        path.write_text(
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "email": "e2e@example.com",
                    "orgId": "org-e2e",
                    "orgName": "E2E Org",
                    "subscriptionType": "max",
                }
            )
        )
        print("\nLogin successful.")
        return 0
    return 2


# -- the stream-json session -----------------------------------------------------------


class Session:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.deferred: list[dict[str, Any]] = []
        self.rpc_id = 0
        self.request_id = 0
        self.turns = 0
        self.calls = 0
        self.system = self._file("--system-prompt-file")
        self.append = self._file("--append-system-prompt-file")
        self.tools: list[str] = []

    def _flag(self, name: str) -> str | None:
        if name in self.argv:
            index = self.argv.index(name)
            return self.argv[index + 1] if index + 1 < len(self.argv) else ""
        return None

    def _file(self, flag: str) -> str | None:
        path = self._flag(flag)
        return Path(path).read_text() if path else None

    def start(self) -> None:
        threading.Thread(target=self._read, daemon=True).start()
        resumed = self._resume()
        log(
            "spawn",
            argv=self.argv,
            cwd=os.getcwd(),
            env={k: v for k, v in os.environ.items() if _interesting(k)},
            system=self.system,
            append=self.append,
            resume=resumed,
        )

    def _resume(self) -> list[dict[str, Any]] | None:
        session_id = self._flag("--resume")
        if session_id is None:
            return None
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        folder = base / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(os.getcwd()))
        path = folder / f"{session_id}.jsonl"
        if not path.exists():
            log("resume_missing", path=str(path))
            sys.stderr.write(f"No conversation found with session ID: {session_id}\n")
            sys.exit(1)
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _read(self) -> None:
        for line in sys.stdin:
            if line.strip():
                self.inbox.put(json.loads(line))
        self.inbox.put(None)

    def next_frame(self) -> dict[str, Any]:
        frame = self.inbox.get()
        if frame is None:
            log("exit", reason="stdin closed")
            sys.exit(0)
        return frame

    # -- control channel ------------------------------------------------------------

    def rpc(
        self, method: str, params: dict[str, Any] | None = None, *, notify: bool = False
    ) -> Any:
        self.request_id += 1
        request_id = f"fake-{PID}-{self.request_id}"
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self.rpc_id += 1
            message["id"] = self.rpc_id
        emit(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": "mcp_message", "server_name": "hx", "message": message},
            }
        )
        while True:
            frame = self.next_frame()
            response = frame.get("response") or {}
            if frame.get("type") == "control_response" and response.get("request_id") == request_id:
                if response.get("subtype") != "success":
                    raise RuntimeError(f"control error: {response}")
                return response["response"]["mcp_response"].get("result")
            self.deferred.append(frame)

    def take(self) -> dict[str, Any]:
        return self.deferred.pop(0) if self.deferred else self.next_frame()

    # -- the run ----------------------------------------------------------------------

    def run(self) -> None:
        self.start()
        while True:
            frame = self.take()
            if frame.get("type") == "control_request":
                self.control(frame)
            elif frame.get("type") == "user":
                content = frame["message"]["content"]
                log("prompt", content=content, client_composed=frame.get("client_composed"))
                self.turn_loop()

    def control(self, frame: dict[str, Any]) -> None:
        request = frame["request"]
        if request.get("subtype") == "initialize":
            emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": frame["request_id"],
                        "response": {
                            "commands": [],
                            "models": models(),
                            "account": {
                                "email": "e2e@example.com",
                                "subscriptionType": "Claude Max",
                            },
                        },
                    },
                }
            )
            if "--mcp-config" in self.argv:
                self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}})
                self.rpc("notifications/initialized", notify=True)
                listed = self.rpc("tools/list", {})
                self.tools = [tool["name"] for tool in listed["tools"]]
                log("tools", tools=listed["tools"])
            emit(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": f"fake-{PID}",
                    "tools": self.tools,
                }
            )
        else:
            log("control", request=request)

    def turn_loop(self) -> None:
        while True:
            turn = self.next_turn()
            if turn is None:
                return
            calls = self.stream(turn)
            if not calls:
                emit(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": turn.get("text", ""),
                        "session_id": f"fake-{PID}",
                        "modelUsage": self.model_usage(),
                    }
                )
                return
            results = []
            for call_id, name, arguments in calls:
                result = self.rpc(
                    "tools/call",
                    {
                        "name": name,
                        "arguments": arguments,
                        "_meta": {"claudecode/toolUseId": call_id},
                    },
                )
                log("result", id=call_id, name=name, result=result)
                results.append(
                    {"tool_use_id": call_id, "type": "tool_result", "content": result["content"]}
                )
            emit(
                {
                    "type": "user",
                    "message": {"role": "user", "content": results},
                    "parent_tool_use_id": None,
                }
            )

    def model_usage(self) -> dict[str, Any]:
        """What Claude Code reports per model at the end of a turn, limits included.

        Keyed by the resolved name, as Claude Code keys it, whether it was
        started with an alias or the full name - with ``[1m]`` kept when the
        model was asked for at its long context, and ``canonicalModel`` without."""
        wanted = self._flag("--model") or "default"
        resolved = next(
            (m["resolvedModel"] for m in models() if wanted in (m["value"], m["resolvedModel"])),
            wanted,
        )
        canonical = re.sub(r"-\d{8}$", "", resolved.removesuffix("[1m]"))
        key = f"{canonical}[1m]" if wanted.endswith("[1m]") else canonical
        window, output = limits().get(key, DEFAULT_LIMITS)
        return {
            key: {
                "inputTokens": 10,
                "outputTokens": 20,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "contextWindow": window,
                "maxOutputTokens": output,
                "canonicalModel": canonical,
            }
        }

    def next_turn(self) -> dict[str, Any] | None:
        if self.system and self.system.strip() == TITLE_SYSTEM:
            title = (STATE / "title").read_text() if (STATE / "title").exists() else "E2E session"
            return {"text": title, "title": True}
        if self.system and self.system.strip() == SUMMARY_SYSTEM:
            log("summary")
            return {"text": "## Goal\nRead the files.\n## Progress\nSome were read."}
        script = STATE / "script.json"
        with (STATE / "cursor.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            turns = json.loads(script.read_text()) if script.exists() else []
            cursor_file = STATE / "cursor"
            cursor = int(cursor_file.read_text()) if cursor_file.exists() else 0
            if cursor >= len(turns):
                log("unexpected", detail="no scripted turn left")
                emit(
                    {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "errors": ["fake: no scripted turn"],
                    }
                )
                return None
            cursor_file.write_text(str(cursor + 1))
        turn = turns[cursor]
        log("turn", index=cursor)
        return turn

    def stream(self, turn: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
        if turn.get("crash"):
            sys.stderr.write(str(turn["crash"]) + "\n")
            sys.stderr.flush()
            log("exit", reason="crash")
            os._exit(1)
        # One frame per window, as Claude Code sends them, ahead of the answer.
        for info in turn.get("rate_limits") or []:
            emit({"type": "rate_limit_event", "rate_limit_info": info})
        if turn.get("rate_limit"):
            emit(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {"status": "rejected", **turn["rate_limit"]},
                }
            )
        if turn.get("error"):
            # An API failure: the CLI says it in a synthetic assistant message,
            # then reports a success-shaped result with is_error set, and the
            # HTTP status beside it when it had one.
            emit(
                {
                    "type": "assistant",
                    "error": "rate_limit" if turn.get("rate_limit") else "unknown",
                    "message": {
                        "model": "<synthetic>",
                        "role": "assistant",
                        "stop_reason": "stop_sequence",
                        "content": [{"type": "text", "text": turn["error"]}],
                    },
                    "parent_tool_use_id": None,
                }
            )
            frame = {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": turn["error"],
            }
            if turn.get("api_error_status"):
                frame["api_error_status"] = turn["api_error_status"]
            emit(frame)
            return []
        self.turns += 1
        usage = turn.get("usage") or {}
        message_id = f"msg_fake_{PID}_{self.turns}"
        emit_event(
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "model": "claude-fake",
                    "role": "assistant",
                    "content": [],
                    "usage": {
                        "input_tokens": usage.get("input", 10),
                        "cache_creation_input_tokens": usage.get("cache_write", 0),
                        "cache_read_input_tokens": usage.get("cache_read", 0),
                        "output_tokens": 1,
                    },
                },
            }
        )
        index = 0
        if turn.get("thinking"):
            emit_event(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                }
            )
            emit_event(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "thinking_delta", "thinking": turn["thinking"]},
                }
            )
            emit_event(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "signature_delta", "signature": f"sig-{message_id}"},
                }
            )
            emit_event({"type": "content_block_stop", "index": index})
            index += 1
        text = turn.get("text") or ""
        if text:
            emit_event(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                }
            )
            for chunk in _chunks(text, int(turn.get("chunks", 3))):
                emit_event(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": chunk},
                    }
                )
                if turn.get("chunk_delay"):
                    time.sleep(float(turn["chunk_delay"]))
            emit_event({"type": "content_block_stop", "index": index})
            index += 1
        calls = []
        for call in turn.get("calls") or []:
            self.calls += 1
            call_id = call.get("id") or f"toolu_fake_{PID}_{self.calls}"
            name = call["name"]
            arguments = call.get("input") or {}
            emit_event(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": call_id,
                        "name": f"mcp__hx__{name}",
                        "input": {},
                    },
                }
            )
            encoded = json.dumps(arguments)
            half = len(encoded) // 2
            for part in (encoded[:half], encoded[half:]):
                emit_event(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": part},
                    }
                )
            emit_event({"type": "content_block_stop", "index": index})
            log("call", id=call_id, name=name, input=arguments)
            calls.append((call_id, name, arguments))
            index += 1
        stop = "tool_use" if calls else turn.get("stop", "end_turn")
        emit_event(
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop},
                "usage": {"output_tokens": usage.get("output", 20)},
            }
        )
        emit_event({"type": "message_stop"})
        return calls


def emit_event(event: dict[str, Any]) -> None:
    emit({"type": "stream_event", "event": event, "parent_tool_use_id": None})


def _chunks(text: str, count: int) -> list[str]:
    size = max(1, -(-len(text) // max(1, count)))
    return [text[i : i + size] for i in range(0, len(text), size)]


def _interesting(name: str) -> bool:
    return name.startswith(
        ("ANTHROPIC_", "CLAUDE", "ENABLE_", "DISABLE_", "MAX_MCP", "MCP_", "NODE_")
    ) or name in {
        "HX_E2E_MARKER",
        "PWD",
    }


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["auth"]:
        return auth(argv[1:])
    if argv[:1] in (["--version"], ["-v"]):
        log("version", path=sys.argv[0])
        print(f"{version()} (Claude Code)")
        return 0
    if "-p" not in argv:
        print("fake claude: only -p and auth are scripted", file=sys.stderr)
        return 2

    def terminated(signum: int, _frame: Any) -> None:
        log("exit", reason=f"signal {signum}")
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, terminated)
    Session(argv).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
