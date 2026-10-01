"""A stand-in for OpenRouter, served over real HTTP.

HX is pointed at it with ``HX_OPENROUTER_BASE_URL``, so everything between the
keyboard and the socket is the shipped code: the request body HX builds, the
SSE parser, retries, usage and cost accounting. Only the model is scripted.

A test queues one :class:`Reply` per completion it expects. Two kinds of call
are answered without the script, because they are HX's own bookkeeping and
not part of the conversation a test is describing: the catalogue
(``GET /models``) and the one-off request that names a session.

Anything the script did not expect is answered with a 500 and recorded in
:attr:`Stub.unexpected`; the suite fails the test on it, so a turn that ran
more model calls than the test described cannot pass by accident.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TITLE_SYSTEM = "You name things briefly and precisely."
"""The system prompt of HX's session-naming call (``hx.core.title``)."""

SONNET = "anthropic/claude-sonnet-4.5"
GPT5 = "openai/gpt-5"
TEXT_ONLY = "deepseek/deepseek-chat"

CATALOGUE: list[dict[str, Any]] = [
    {
        "id": SONNET,
        "name": "Anthropic: Claude Sonnet 4.5",
        "context_length": 200_000,
        "top_provider": {"context_length": 200_000, "max_completion_tokens": 64_000},
        "pricing": {
            "prompt": "0.000003",
            "completion": "0.000015",
            "input_cache_read": "0.0000003",
            "input_cache_write": "0.00000375",
        },
        "supported_parameters": ["tools", "reasoning"],
        "architecture": {"input_modalities": ["text", "image"]},
    },
    {
        "id": GPT5,
        "name": "OpenAI: GPT-5",
        "context_length": 400_000,
        "top_provider": {"context_length": 400_000, "max_completion_tokens": 128_000},
        "pricing": {"prompt": "0.00000125", "completion": "0.00001"},
        "supported_parameters": ["tools", "reasoning"],
        "architecture": {"input_modalities": ["text", "image"]},
    },
    {
        "id": TEXT_ONLY,
        "name": "DeepSeek: DeepSeek V3",
        "context_length": 64_000,
        "top_provider": {"context_length": 64_000, "max_completion_tokens": 8_000},
        "pricing": {"prompt": "0.0000003", "completion": "0.0000012"},
        "supported_parameters": ["tools"],
        "architecture": {"input_modalities": ["text"]},
    },
]


@dataclass(slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""


@dataclass(slots=True)
class Reply:
    """One scripted completion.

    ``hold`` parks the stream after ``hold_after`` characters of text have been
    sent, until the test sets it - which is how a test catches a turn in the
    act, to interrupt or steer it. ``status`` other than 200 answers with an
    OpenRouter-shaped error instead of a stream.
    """

    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 1_200
    completion_tokens: int = 40
    cached_tokens: int = 0
    cost: float | None = 0.0042
    status: int = 200
    error: str = ""
    hold: threading.Event | None = None
    hold_after: int = 0
    chunk_size: int = 24
    finish_reason: str | None = None


def say(text: str, **kwargs: Any) -> Reply:
    """The model answers in prose and ends its turn."""
    return Reply(text=text, **kwargs)


def call(name: str, text: str = "", /, **arguments: Any) -> Reply:
    """The model calls one tool. Positional-only, so a tool may take ``name`` or ``text``."""
    return Reply(text=text, tool_calls=[ToolCall(name, arguments)])


def calls(*tool_calls: ToolCall, text: str = "") -> Reply:
    """The model calls several tools in one turn."""
    return Reply(text=text, tool_calls=list(tool_calls))


def fail(status: int, message: str) -> Reply:
    """OpenRouter refuses the request."""
    return Reply(status=status, error=message)


@dataclass(slots=True)
class Request:
    """One completion request HX sent, as it arrived on the wire."""

    body: dict[str, Any]
    headers: dict[str, str]

    @property
    def messages(self) -> list[dict[str, Any]]:
        return list(self.body.get("messages") or [])

    @property
    def model(self) -> str:
        return str(self.body.get("model"))

    @property
    def tools(self) -> list[str]:
        return [t["function"]["name"] for t in self.body.get("tools") or []]

    @property
    def system(self) -> str:
        return _text(self.messages[0]["content"]) if self.messages else ""

    def last(self, role: str) -> dict[str, Any]:
        return next(m for m in reversed(self.messages) if m["role"] == role)

    def last_user_text(self) -> str:
        return _text(self.last("user")["content"])

    def tool_results(self) -> list[str]:
        return [str(m["content"]) for m in self.messages if m["role"] == "tool"]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _unanswered_tool_calls(messages: list[dict[str, Any]]) -> list[str]:
    """Tool call ids not answered by the ``tool`` messages that follow their call."""
    unanswered: list[str] = []
    for index, message in enumerate(messages):
        ids = [c.get("id", "") for c in message.get("tool_calls") or []]
        if not ids:
            continue
        answered: set[str] = set()
        for reply in messages[index + 1 :]:
            if reply.get("role") != "tool":
                break
            answered.add(str(reply.get("tool_call_id")))
        unanswered.extend(i for i in ids if i not in answered)
    return unanswered


class Stub:
    """The server, and the script it plays."""

    def __init__(self) -> None:
        self._script: deque[Reply] = deque()
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)
        self.requests: list[Request] = []
        """Every conversation request, in arrival order (not titles or catalogue)."""
        self.title_requests: list[Request] = []
        self.unexpected: list[str] = []
        self.title = "E2E session"
        self.title_hold: threading.Event | None = None
        """When set, a naming call is answered only once this event is - a slow
        title model, caught with the call still out."""
        self.models = list(CATALOGUE)
        self.model_fetches = 0
        self._closing = threading.Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/api/v1"

    def start(self) -> Stub:
        self._thread.start()
        return self

    def close(self) -> None:
        self._closing.set()
        with self._lock:
            for reply in self._script:
                if reply.hold is not None:
                    reply.hold.set()
        self._server.shutdown()
        self._server.server_close()

    def script(self, *replies: Reply) -> None:
        """Queue completions, answered in order."""
        with self._lock:
            self._script.extend(replies)

    @property
    def pending(self) -> int:
        """Scripted replies not yet asked for."""
        with self._lock:
            return len(self._script)

    def wait_for_requests(self, count: int, timeout: float = 15.0) -> list[Request]:
        """Block until ``count`` conversation requests have arrived."""
        deadline = time.monotonic() + timeout
        with self._arrived:
            while len(self.requests) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"expected {count} model request(s), got {len(self.requests)}"
                    )
                self._arrived.wait(remaining)
            return list(self.requests)

    def wait_for_title_requests(self, count: int, timeout: float = 15.0) -> list[Request]:
        """Block until ``count`` session-naming requests have arrived."""
        deadline = time.monotonic() + timeout
        with self._arrived:
            while len(self.title_requests) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"expected {count} title request(s), got {len(self.title_requests)}"
                    )
                self._arrived.wait(remaining)
            return list(self.title_requests)

    def log(self) -> list[dict[str, Any]]:
        """What the report records: each request's model, last user text and tools."""
        return [
            {
                "model": r.model,
                "last_user": _text(r.last("user")["content"])[:400]
                if any(m["role"] == "user" for m in r.messages)
                else "",
                "tool_results": [t[:400] for t in r.tool_results()],
                "tools": r.tools,
            }
            for r in self.requests
        ]

    # -- HTTP --------------------------------------------------------------

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                if self.path.rstrip("/").endswith("/models"):
                    stub.model_fetches += 1
                    self._json(200, {"data": stub.models})
                    return
                self._json(404, {"error": {"message": f"no route {self.path}"}})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self._json(404, {"error": {"message": f"no route {self.path}"}})
                    return
                request = Request(body=body, headers=dict(self.headers.items()))
                if request.system == TITLE_SYSTEM:
                    with stub._arrived:
                        stub.title_requests.append(request)
                        stub._arrived.notify_all()
                    self._stream(
                        Reply(
                            text=stub.title,
                            prompt_tokens=300,
                            completion_tokens=5,
                            hold=stub.title_hold,
                        )
                    )
                    return
                if unanswered := _unanswered_tool_calls(request.messages):
                    # Every upstream rejects a tool call left without its
                    # result - OpenAI, Anthropic and the Codex Responses
                    # backend alike - so the stub does too, in OpenAI's words.
                    stub.unexpected.append(f"tool calls without results: {unanswered}")
                    self._json(
                        400,
                        {
                            "error": {
                                "message": "An assistant message with 'tool_calls' must be "
                                "followed by tool messages responding to each "
                                "'tool_call_id'. The following tool_call_ids did not have "
                                f"response messages: {', '.join(unanswered)}"
                            }
                        },
                    )
                    return
                with stub._arrived:
                    stub.requests.append(request)
                    reply = stub._script.popleft() if stub._script else None
                    if reply is None:
                        stub.unexpected.append(
                            f"model request #{len(stub.requests)} with no scripted reply "
                            f"(last user message: {request.last_user_text()[:200]!r})"
                        )
                    stub._arrived.notify_all()
                if reply is None:
                    self._json(500, {"error": {"message": "stub: no scripted reply"}})
                    return
                if reply.status != 200:
                    self._json(reply.status, {"error": {"message": reply.error}})
                    return
                self._stream(reply)

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _event(self, payload: dict[str, Any]) -> None:
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()

            def _stream(self, reply: Reply) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                # OpenRouter's keep-alive comment, which a client must skip.
                self.wfile.write(b": OPENROUTER PROCESSING\n\n")
                try:
                    self._play(reply)
                except (BrokenPipeError, ConnectionResetError):
                    # HX hung up mid-stream: an interrupt, or a steer.
                    return

            def _play(self, reply: Reply) -> None:
                def delta(**fields: Any) -> None:
                    self._event({"choices": [{"index": 0, "delta": fields}]})

                if reply.reasoning:
                    delta(reasoning=reply.reasoning)
                size = reply.chunk_size
                head, tail = reply.text, ""
                if reply.hold is not None:
                    head, tail = reply.text[: reply.hold_after], reply.text[reply.hold_after :]
                for start in range(0, len(head), size):
                    delta(content=head[start : start + size])
                if reply.hold is not None:
                    self._park(reply.hold)
                for start in range(0, len(tail), size):
                    delta(content=tail[start : start + size])
                for index, tool in enumerate(reply.tool_calls):
                    arguments = json.dumps(tool.arguments)
                    middle = len(arguments) // 2
                    # Arguments arrive split across chunks, as they do upstream.
                    delta(
                        tool_calls=[
                            {
                                "index": index,
                                "id": tool.id or f"call_{index}_{tool.name}",
                                "type": "function",
                                "function": {"name": tool.name, "arguments": arguments[:middle]},
                            }
                        ]
                    )
                    delta(
                        tool_calls=[{"index": index, "function": {"arguments": arguments[middle:]}}]
                    )
                finish = reply.finish_reason or ("tool_calls" if reply.tool_calls else "stop")
                self._event({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
                usage: dict[str, Any] = {
                    "prompt_tokens": reply.prompt_tokens,
                    "completion_tokens": reply.completion_tokens,
                    "prompt_tokens_details": {"cached_tokens": reply.cached_tokens},
                }
                if reply.cost is not None:
                    usage["cost"] = reply.cost
                self._event({"choices": [], "usage": usage})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def _park(self, hold: threading.Event) -> None:
                # A parked stream still has to notice a client that went away,
                # or an interrupted turn leaves this thread waiting forever.
                while not hold.wait(0.05):
                    if stub._closing.is_set():
                        raise BrokenPipeError
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()

        return Handler
