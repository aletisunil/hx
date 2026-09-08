"""MCP client - stdio and streamable HTTP transports.

Implements the subset HX needs: ``initialize``, ``tools/list``, ``tools/call``.
JSON-RPC 2.0 over either transport.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "hx", "version": "0.0.1"}

log = logging.getLogger(__name__)


@dataclass(slots=True)
class MCPToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True)
class ServerCapabilities:
    tools: bool = False
    prompts: bool = False
    resources: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class Transport(abc.ABC):
    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def send(self, message: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    def receive(self) -> AsyncIterator[dict[str, Any]]: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


class StdioTransport(Transport):
    """Newline-delimited JSON-RPC over a subprocess's stdio.

    The server's stderr is drained to the log rather than the terminal; a chatty
    server would otherwise corrupt the TUI render.
    """

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.command = command
        self.args = list(args)
        self.env = env
        self.cwd = cwd
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        import os

        self._process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(self.env or {})},
            cwd=str(self.cwd) if self.cwd else None,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._process is not None
        if self._process.stderr is None:
            return
        async for line in self._process.stderr:
            log.debug("mcp[%s] %s", self.command, line.decode(errors="replace").rstrip())

    async def send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise MCPError("transport is not connected")
        self._process.stdin.write((json.dumps(message) + "\n").encode())
        await self._process.stdin.drain()

    async def receive(self) -> AsyncIterator[dict[str, Any]]:
        if self._process is None or self._process.stdout is None:
            raise MCPError("transport is not connected")
        async for raw in self._process.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # A server printing plain text on stdout is a server bug, not a
                # reason to tear down the session.
                log.warning("mcp[%s] non-JSON on stdout: %s", self.command, line[:200])

    async def close(self) -> None:
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        if self._process is None:
            return
        with contextlib.suppress(ProcessLookupError):
            self._process.terminate()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(self._process.wait(), timeout=5)
        self._process = None


class HTTPTransport(Transport):
    """Streamable HTTP transport. Responses arrive as JSON or as an SSE stream."""

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        }
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))

    async def send(self, message: dict[str, Any]) -> None:
        if self._client is None:
            raise MCPError("transport is not connected")
        headers = dict(self.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        response = await self._client.post(self.url, json=message, headers=headers)
        if response.status_code >= 400:
            raise MCPError(f"{self.url} returned {response.status_code}: {response.text[:200]}")
        if session_id := response.headers.get("Mcp-Session-Id"):
            self._session_id = session_id

        if response.status_code == 202 or not response.content:
            return
        for payload in _decode_http_body(response):
            await self._inbox.put(payload)

    async def receive(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await self._inbox.get()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _decode_http_body(response: httpx.Response) -> list[dict[str, Any]]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        payloads = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                with contextlib.suppress(json.JSONDecodeError):
                    payloads.append(json.loads(line[5:].strip()))
        return payloads
    try:
        body = response.json()
    except ValueError as exc:
        raise MCPError(f"invalid JSON from {response.url}: {exc}") from exc
    return body if isinstance(body, list) else [body]


class MCPClient:
    """One connected server."""

    def __init__(self, name: str, transport: Transport, timeout: float = 30.0) -> None:
        self.name = name
        self.transport = transport
        self.timeout = timeout
        self.capabilities = ServerCapabilities()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None

    async def initialize(self) -> ServerCapabilities:
        await self.transport.connect()
        self._reader = asyncio.create_task(self._read_loop())

        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        raw = result.get("capabilities") or {}
        self.capabilities = ServerCapabilities(
            tools="tools" in raw,
            prompts="prompts" in raw,
            resources="resources" in raw,
            raw=raw,
        )
        await self._notify("notifications/initialized", {})
        return self.capabilities

    async def list_tools(self) -> list[MCPToolDef]:
        if not self.capabilities.tools:
            return []
        result = await self._request("tools/list", {})
        return [
            MCPToolDef(
                name=str(item["name"]),
                description=str(item.get("description") or ""),
                input_schema=item.get("inputSchema") or {"type": "object", "properties": {}},
            )
            for item in result.get("tools", [])
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call a tool and flatten the content blocks to text.

        Tool results are data from a third-party server. They are returned to the
        model as tool output and must never be treated as instructions to HX.
        """
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        parts = [
            str(block.get("text", ""))
            for block in result.get("content", [])
            if block.get("type") == "text"
        ]
        text = "\n".join(part for part in parts if part)
        if result.get("isError"):
            raise MCPToolError(text or f"{name} failed")
        return text or "(no content)"

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        await self.transport.send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            message = await asyncio.wait_for(future, timeout=self.timeout)
        except TimeoutError as exc:
            raise MCPError(f"{self.name}: {method} timed out after {self.timeout:.0f}s") from exc
        finally:
            self._pending.pop(request_id, None)

        if error := message.get("error"):
            raise MCPError(f"{self.name}: {error.get('message', error)}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self.transport.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _read_loop(self) -> None:
        try:
            async for message in self.transport.receive():
                request_id = message.get("id")
                future = self._pending.get(request_id) if request_id is not None else None
                if future is not None and not future.done():
                    future.set_result(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_pending(f"transport failed: {exc}")
            return

        # The stream ended: the server exited. Fail the waiters now rather than
        # letting each one burn its full timeout against a process that is gone.
        self._fail_pending("the server exited")

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPError(f"{self.name}: {reason}"))

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        await self.transport.close()


class MCPError(Exception):
    pass


class MCPToolError(MCPError):
    """The server ran the tool and it failed - distinct from a protocol failure."""
