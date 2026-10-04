"""One running ``claude`` process, spoken to over its stream-json protocol.

Claude Code in print mode with ``--input-format stream-json`` and
``--output-format stream-json`` reads newline-delimited JSON on stdin and
writes it on stdout. Three kinds of traffic share the two pipes:

* conversation frames - ``user`` messages in; ``stream_event`` (raw Anthropic
  stream events), ``assistant``, ``user`` (tool results it recorded),
  ``system`` and ``result`` out;
* control requests either way, each answered by a ``control_response`` carrying
  the same ``request_id`` - HX sends ``initialize`` and ``interrupt``;
* MCP over the control channel: a server declared ``{"type": "sdk"}`` in
  ``--mcp-config`` lives in *this* process, and the CLI reaches it with
  ``mcp_message`` control requests carrying JSON-RPC.

That last one is how HX's tools reach the model. The CLI runs with every
built-in tool off and HX's tools served as one SDK MCP server; a ``tools/call``
from the CLI is answered whenever HX has run the tool - which may be minutes
later, after the user approved it.

This is the protocol the Agent SDKs speak. HX implements the slice it needs
instead of depending on the SDK, which would bring the ``mcp`` package and a
bundled copy of the CLI with it, for a few hundred lines of framing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from hx.providers.base import ProviderError

READ_LIMIT = 256 * 1024 * 1024
"""Longest stdout line accepted. The CLI echoes every tool result back as a
``user`` frame, so one line can be as large as the largest output HX returns."""

STDERR_LINES = 40

INITIALIZE_TIMEOUT = 60.0
SERVER_NAME = "hx"
TOOL_USE_META = "claudecode/toolUseId"
"""Where the CLI puts the id of the ``tool_use`` block a ``tools/call`` serves.

Not part of MCP. Without it a call could only be paired with its block by
order or by arguments, and neither is reliable once calls run concurrently."""

MCP_PROTOCOL_VERSION = "2025-06-18"

ToolHandler = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
"""``(tool_use_id, name, arguments) -> MCP CallToolResult``."""


@dataclass(slots=True)
class ServedTool:
    name: str
    """What the CLI calls it. The model sees ``mcp__hx__<name>``."""
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True)
class ToolServer:
    """The MCP server HX's tools are served from."""

    tools: list[ServedTool]
    handler: ToolHandler

    def answer(
        self, message: dict[str, Any]
    ) -> Awaitable[dict[str, Any] | None] | dict[str, Any] | None:
        """The JSON-RPC response to ``message``; ``None`` for a notification.

        ``tools/call`` returns an awaitable: its answer is whenever HX has run
        the tool. Everything else is answered on the spot.
        """
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return None
        if method == "initialize":
            params = message.get("params") or {}
            return _result(
                request_id,
                {
                    "protocolVersion": params.get("protocolVersion") or MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": "1"},
                },
            )
        if method == "tools/list":
            return _result(
                request_id,
                {
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": tool.input_schema,
                        }
                        for tool in self.tools
                    ]
                },
            )
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/call":
            return self._call(request_id, message.get("params") or {})
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    async def _call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        meta = params.get("_meta") or {}
        tool_use_id = meta.get(TOOL_USE_META)
        name = str(params.get("name") or "")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            # Refused rather than guessed at: pairing by order would hand one
            # call another's result the first time two of them overlap.
            return _result(
                request_id,
                _error_content(
                    f"{name}: the call carried no tool_use id, so HX cannot pair it with "
                    "its result. Update Claude Code (`claude update`)."
                ),
            )
        arguments = params.get("arguments")
        result = await self.handler(
            tool_use_id, name, arguments if isinstance(arguments, dict) else {}
        )
        return _result(request_id, result)


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_content(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


@dataclass(slots=True)
class Exit:
    """Put on the frame queue when stdout ends."""

    code: int | None
    stderr: str


@dataclass(slots=True)
class CliProcess:
    """A spawned ``claude`` and the tasks that serve its pipes.

    Conversation frames are queued on :attr:`frames` in arrival order, followed
    by one :class:`Exit` when stdout closes. Control requests are answered here
    and never reach the queue.
    """

    process: asyncio.subprocess.Process
    server: ToolServer | None
    frames: asyncio.Queue[dict[str, Any] | Exit] = field(default_factory=asyncio.Queue)
    _stderr: deque[str] = field(default_factory=lambda: deque(maxlen=STDERR_LINES))
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    _handlers: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _closed: bool = False

    @classmethod
    async def spawn(
        cls,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        server: ToolServer | None,
    ) -> CliProcess:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                limit=READ_LIMIT,
                # Its own process group: Ctrl+C in HX's terminal is HX's to
                # handle, and must not reach the CLI behind HX's back.
                start_new_session=True,
            )
        except OSError as exc:
            raise ProviderError(f"Could not start Claude Code ({argv[0]}): {exc}") from exc
        cli = cls(process=process, server=server)
        cli._tasks.append(asyncio.ensure_future(cli._read_stdout()))
        cli._tasks.append(asyncio.ensure_future(cli._read_stderr()))
        return cli

    @property
    def alive(self) -> bool:
        return not self._closed and self.process.returncode is None

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    # -- writing -------------------------------------------------------------

    async def send(self, frame: dict[str, Any]) -> None:
        stdin = self.process.stdin
        if stdin is None or stdin.is_closing() or self._closed:
            raise ProviderError("Claude Code is no longer running.")
        data = (json.dumps(frame, ensure_ascii=False) + "\n").encode()
        async with self._write_lock:
            try:
                stdin.write(data)
                await stdin.drain()
            except (ConnectionError, OSError) as exc:
                raise ProviderError(f"Claude Code closed its input: {exc}") from exc

    async def request(
        self, payload: dict[str, Any], *, deadline: float | None = None
    ) -> dict[str, Any]:
        """Send a control request and wait for its response."""
        request_id = f"hx_{uuid.uuid4().hex[:12]}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.send(
                {"type": "control_request", "request_id": request_id, "request": payload}
            )
            return await asyncio.wait_for(future, deadline)
        except TimeoutError as exc:
            raise ProviderError(
                f"Claude Code did not answer {payload.get('subtype')!r} in {deadline or 0:.0f}s."
                + _stderr_suffix(self.stderr_tail())
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def initialize(self) -> dict[str, Any]:
        return await self.request(
            {"subtype": "initialize", "hooks": None}, deadline=INITIALIZE_TIMEOUT
        )

    async def send_user(self, content: str | list[dict[str, Any]]) -> None:
        """A user turn. ``client_composed`` keeps the CLI from treating the text
        as its own input: no ``@path`` expansion, no slash commands."""
        await self.send(
            {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
                "session_id": "",
                "client_composed": True,
            }
        )

    # -- reading -------------------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                try:
                    line = await self.process.stdout.readline()
                except ValueError:
                    # A line past READ_LIMIT. Nothing after it can be trusted to
                    # be framed correctly, so the process is done.
                    self._stderr.append("stdout line exceeded the read limit")
                    break
                if not line:
                    break
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    self._stderr.append(line.decode(errors="replace").rstrip())
                    continue
                if isinstance(frame, dict):
                    self._route(frame)
        finally:
            code = await self._wait_exit()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        ProviderError(
                            "Claude Code exited before answering."
                            + _stderr_suffix(self.stderr_tail())
                        )
                    )
            self.frames.put_nowait(Exit(code, self.stderr_tail()))

    async def _wait_exit(self) -> int | None:
        with contextlib.suppress(Exception):
            # Stderr is read concurrently; give it a moment to drain so an exit
            # caused by an error says what the error was.
            await asyncio.wait_for(self.process.wait(), 5)
        return self.process.returncode

    async def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        with contextlib.suppress(Exception):
            async for raw in self.process.stderr:
                text = raw.decode(errors="replace").rstrip()
                if text:
                    self._stderr.append(text)

    def _route(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "control_response":
            response = frame.get("response") or {}
            future = self._pending.get(str(response.get("request_id")))
            if future is not None and not future.done():
                if response.get("subtype") == "error":
                    future.set_exception(
                        ProviderError(f"Claude Code refused the request: {response.get('error')}")
                    )
                else:
                    future.set_result(response.get("response") or {})
            return
        if kind == "control_request":
            request_id = str(frame.get("request_id"))
            task = asyncio.ensure_future(self._serve(request_id, frame.get("request") or {}))
            self._handlers[request_id] = task
            task.add_done_callback(lambda _t: self._handlers.pop(request_id, None))
            return
        if kind == "control_cancel_request":
            handler = self._handlers.pop(str(frame.get("request_id")), None)
            if handler is not None:
                handler.cancel()
            return
        self.frames.put_nowait(frame)

    async def _serve(self, request_id: str, request: dict[str, Any]) -> None:
        subtype = request.get("subtype")
        try:
            if subtype == "mcp_message" and self.server is not None:
                if request.get("server_name") != SERVER_NAME:
                    raise ValueError(f"unknown MCP server {request.get('server_name')!r}")
                message = request.get("message") or {}
                answer = self.server.answer(message)
                if asyncio.iscoroutine(answer) or isinstance(answer, asyncio.Future):
                    answer = await answer
                payload = {"mcp_response": answer or {"jsonrpc": "2.0", "result": {}}}
            else:
                raise ValueError(f"unsupported control request {subtype!r}")
        except asyncio.CancelledError:
            # The CLI gave up on this request; it expects no answer.
            raise
        except Exception as exc:
            with contextlib.suppress(ProviderError):
                await self.send(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "error",
                            "request_id": request_id,
                            "error": str(exc),
                        },
                    }
                )
            return
        with contextlib.suppress(ProviderError):
            await self.send(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": payload,
                    },
                }
            )

    # -- ending --------------------------------------------------------------

    async def close(self) -> None:
        """Stop the process and everything serving it. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        for handler in list(self._handlers.values()):
            handler.cancel()
        process = self.process
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                # The whole group: the CLI's own children (a hook, a helper)
                # must not outlive it.
                os.killpg(process.pid, 15)
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, 9)
                await process.wait()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()


def _stderr_suffix(stderr: str) -> str:
    tail = stderr.strip()
    return f"\n{tail[-1500:]}" if tail else ""
