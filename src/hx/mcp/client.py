"""MCP client - stdio and streamable HTTP transports.

Implements the subset HX needs: ``initialize``, ``tools/list``, ``tools/call``,
and ``notifications/tools/list_changed``. JSON-RPC 2.0 over either transport.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from hx import __version__
from hx.net import async_client

if TYPE_CHECKING:
    from hx.mcp.oauth import OAuthSession

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "hx", "version": __version__}

log = logging.getLogger(__name__)


@dataclass(slots=True)
class MCPToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True)
class MCPToolOutput:
    """What a ``tools/call`` returned, reduced to what the model can be shown."""

    text: str
    images: list[tuple[str, str]] = field(default_factory=list)
    """``(base64 data, mime type)`` per ``image`` content block, as sent."""


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

    def set_protocol_version(self, version: str) -> None:
        """The version ``initialize`` settled on. Only HTTP has to repeat it."""
        return

    def listen(self) -> None:
        """Start taking messages the server sends outside a request.

        A no-op where every message already arrives on one stream, as on stdio.
        """
        return


STDIO_LINE_LIMIT = 16 * 1024 * 1024
"""Longest JSON-RPC line a server may send.

One message is one line, and a tool that returns a file or a search result puts
the whole thing on it. ``asyncio``'s stream default is 64 KiB, which a real MCP
server passes routinely - and overshooting it does not truncate the message, it
raises out of the read loop and takes the connection down for the rest of the
session. The ceiling is here so that a server which streams something genuinely
unbounded still fails instead of growing the buffer forever.
"""


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
            limit=STDIO_LINE_LIMIT,
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
        stdout = self._process.stdout
        while True:
            try:
                raw = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                # Over the line limit. The reader is parked mid-message with no
                # way to find the next boundary, so the connection is finished -
                # but it says which server and why, rather than surfacing as a
                # bare "Separator is not found" from inside asyncio.
                raise MCPError(
                    f"{self.command}: a message exceeded the "
                    f"{STDIO_LINE_LIMIT // (1024 * 1024)} MiB line limit"
                ) from exc
            if not raw:
                return
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
    """Streamable HTTP transport.

    Requests are POSTed; each reply arrives as JSON or as an SSE stream, which
    may carry notifications ahead of the response. Messages the server sends on
    its own - a changed tool list, say - come down a GET stream held open
    beside them, when the server offers one.

    With ``auth``, requests carry the server's OAuth token, a ``401`` is met
    with one refresh and one retry, and a server that still refuses raises
    :class:`MCPAuthRequired` for the caller to turn into "sign in".
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        auth: OAuthSession | None = None,
    ) -> None:
        self.url = url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        }
        self.auth = auth
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._listener: asyncio.Task[None] | None = None
        self._listening_to: str | None = None
        """The session the GET stream was opened for. A stream is one
        session's, so a new session needs a new one."""

    async def connect(self) -> None:
        self._client = async_client(timeout=httpx.Timeout(120.0, connect=15.0))

    def set_protocol_version(self, version: str) -> None:
        self._protocol_version = version

    async def send(self, message: dict[str, Any]) -> None:
        if self._client is None:
            raise MCPError("transport is not connected")
        # `initialize` starts a session, so it never names one - a server
        # refuses to initialize a session twice.
        session = None if message.get("method") == "initialize" else self._session_id
        response = await self._authorized(
            lambda headers: self._client.post(self.url, json=message, headers=headers),  # type: ignore[union-attr]
            session,
        )
        if response.status_code == 404 and session is not None:
            # The server forgot the session - a restart, or an idle timeout.
            # Only a fresh `initialize` can start another. Forgotten only if it
            # is still the current one: a request that went out beside the one
            # that already started the next session must not end that too.
            if self._session_id == session:
                self._session_id = None
            raise MCPSessionExpired(f"{self.url}: the server ended the session")
        if response.status_code >= 400:
            raise MCPError(f"{self.url} returned {response.status_code}: {response.text[:200]}")
        if session_id := response.headers.get("Mcp-Session-Id"):
            self._session_id = session_id

        if response.status_code == 202 or not response.content:
            return
        for payload in _decode_http_body(response):
            await self._inbox.put(payload)

    async def _authorized(
        self,
        request: Callable[[dict[str, str]], Awaitable[httpx.Response]],
        session: str | None,
    ) -> httpx.Response:
        """Make ``request`` with the current token, recovering once from a 401."""
        token = await self._token()
        response = await request(self._request_headers(token, session))
        if response.status_code != 401 or self.auth is None:
            return response
        try:
            fresh = await self.auth.recover(token)
        except httpx.HTTPError as exc:
            raise MCPError(f"{self.url}: could not refresh the sign-in: {exc}") from exc
        if fresh is not None:
            response = await request(self._request_headers(fresh, session))
            if response.status_code != 401:
                return response
        raise MCPAuthRequired(
            "needs sign-in"
            if token is None
            else "needs sign-in: the last one expired or was revoked"
        )

    async def _token(self) -> str | None:
        if self.auth is None:
            return None
        try:
            return await self.auth.token()
        except httpx.HTTPError as exc:
            raise MCPError(f"{self.url}: could not refresh the sign-in: {exc}") from exc

    def _request_headers(self, token: str | None, session: str | None) -> dict[str, str]:
        headers = dict(self.headers)
        if session:
            headers["Mcp-Session-Id"] = session
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def receive(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await self._inbox.get()

    def listen(self) -> None:
        """Open the GET stream for the current session, unless one is open.

        Called after every ``initialize``: the stream belongs to the session
        it was opened under, and ends with it.
        """
        if self._client is None:
            return
        if self._listener is not None and not self._listener.done():
            if self._listening_to == self._session_id:
                return
            self._listener.cancel()
        self._listening_to = self._session_id
        self._listener = asyncio.create_task(self._listen(self._session_id))

    async def _listen(self, session: str | None) -> None:
        """Hold ``session``'s GET stream open, reconnecting when it drops.

        Optional in the spec: a server answering 405 has nothing to say outside
        a request, and is left alone for the rest of the session. So is a
        session the server no longer knows (404) - the next ``initialize``
        opens a stream for its successor. A stream that drops is reopened with
        backoff, reset only once one has carried a message - a server that
        closes every stream straight away must not be polled once a second for
        the rest of the session.
        """
        backoff = 1.0
        last_event_id: str | None = None
        recovered = False
        while self._client is not None and self._session_id == session:
            try:
                token = await self._token()
                headers = self._request_headers(token, session)
                headers["Accept"] = "text/event-stream"
                if last_event_id:
                    headers["Last-Event-ID"] = last_event_id
                async with self._client.stream(
                    "GET", self.url, headers=headers, timeout=httpx.Timeout(15.0, read=None)
                ) as response:
                    if response.status_code == 401 and self.auth is not None:
                        # One refresh per refusal. A server that rejects even a
                        # fresh token would otherwise have this loop spend a
                        # refresh token on every pass.
                        if recovered or await self.auth.recover(token) is None:
                            return
                        recovered = True
                        continue
                    if response.status_code >= 400 or "text/event-stream" not in (
                        response.headers.get("content-type", "")
                    ):
                        log.debug("mcp %s: no server stream (%s)", self.url, response.status_code)
                        return
                    recovered = False
                    decoder = SSEDecoder()
                    async for line in response.aiter_lines():
                        payload = decoder.feed(line)
                        last_event_id = decoder.last_event_id or last_event_id
                        if payload is not None:
                            backoff = 1.0
                            await self._inbox.put(payload)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, MCPError) as exc:
                log.debug("mcp %s: server stream dropped: %s", self.url, exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def close(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener
            self._listener = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class SSEDecoder:
    """Server-sent events, one line at a time, yielding each JSON message.

    An event's ``data:`` lines are joined with newlines and end at a blank
    line, per the SSE spec - a server that splits one JSON message across
    several ``data:`` lines is within its rights.
    """

    def __init__(self) -> None:
        self._data: list[str] = []
        self.last_event_id: str | None = None

    def feed(self, line: str) -> dict[str, Any] | None:
        line = line.rstrip("\r")
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            self._data.append(value)
        elif name == "id":
            self.last_event_id = value or None
        return None

    def flush(self) -> dict[str, Any] | None:
        return self._dispatch()

    def _dispatch(self) -> dict[str, Any] | None:
        if not self._data:
            return None
        text = "\n".join(self._data)
        self._data = []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            log.debug("mcp: non-JSON event: %s", text[:200])
            return None
        return payload if isinstance(payload, dict) else None


def _decode_http_body(response: httpx.Response) -> list[dict[str, Any]]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        decoder = SSEDecoder()
        payloads = [p for line in response.text.splitlines() if (p := decoder.feed(line))]
        if (last := decoder.flush()) is not None:
            payloads.append(last)
        return payloads
    try:
        body = response.json()
    except ValueError as exc:
        raise MCPError(f"invalid JSON from {response.url}: {exc}") from exc
    return body if isinstance(body, list) else [body]


class MCPClient:
    """One connected server."""

    def __init__(
        self,
        name: str,
        transport: Transport,
        timeout: float = 30.0,
        on_tools_changed: Callable[[], None] | None = None,
    ) -> None:
        self.name = name
        self.transport = transport
        self.timeout = timeout
        self.on_tools_changed = on_tools_changed
        """Called when the server says its tool list changed. Must not block:
        it runs on the read loop, which the follow-up ``tools/list`` needs."""
        self.capabilities = ServerCapabilities()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._replies: set[asyncio.Task[None]] = set()
        self._generation = 0
        """How many sessions ``initialize`` has started."""
        self._renewing = asyncio.Lock()

    async def initialize(self) -> ServerCapabilities:
        await self.transport.connect()
        self._reader = asyncio.create_task(self._read_loop())
        await self._handshake()
        return self.capabilities

    async def _handshake(self) -> None:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            reinitialize=False,
        )
        self.transport.set_protocol_version(str(result.get("protocolVersion") or PROTOCOL_VERSION))
        raw = result.get("capabilities") or {}
        self.capabilities = ServerCapabilities(
            tools="tools" in raw,
            prompts="prompts" in raw,
            resources="resources" in raw,
            raw=raw,
        )
        await self._notify("notifications/initialized", {})
        self._generation += 1
        self.transport.listen()

    async def _renew_session(self, expired: int) -> None:
        """Start a session in place of the one generation ``expired`` used.

        Once, however many requests found it gone: the rest wait here and then
        use the session the first one started.
        """
        async with self._renewing:
            if self._generation != expired:
                return
            log.info("mcp %s: session expired; initializing again", self.name)
            await self._handshake()

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

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolOutput:
        """Call a tool and reduce its content blocks to text and images.

        Tool results are data from a third-party server. They are returned to the
        model as tool output and must never be treated as instructions to HX.
        Audio and embedded resources have no route to the model and are dropped.
        """
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        parts: list[str] = []
        images: list[tuple[str, str]] = []
        for block in result.get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "image" and isinstance(block.get("data"), str):
                images.append((block["data"], str(block.get("mimeType") or "")))
        text = "\n".join(part for part in parts if part)
        if result.get("isError"):
            raise MCPToolError(text or f"{name} failed")
        if not text:
            text = (
                f"({len(images)} image{'s' if len(images) != 1 else ''})"
                if images
                else "(no content)"
            )
        return MCPToolOutput(text=text, images=images)

    async def _request(
        self, method: str, params: dict[str, Any], *, reinitialize: bool = True
    ) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        generation = self._generation
        try:
            try:
                await self.transport.send(request)
            except MCPSessionExpired:
                if not reinitialize:
                    raise
                # Once, and then the request again: a long session outlives the
                # server's idea of it, and the user should not have to restart HX.
                await self._renew_session(generation)
                await self.transport.send(request)
        except BaseException:
            self._pending.pop(request_id, None)
            raise
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
                if "method" in message:
                    self._from_server(message)
                    continue
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

    def _from_server(self, message: dict[str, Any]) -> None:
        """A notification or request the server started, as opposed to a reply.

        Told apart by ``method``, not by ``id``: a server's own request carries
        an id from its numbering, which can collide with one of ours.
        """
        method = message.get("method")
        if "id" not in message:
            if method == "notifications/tools/list_changed" and self.on_tools_changed:
                self.on_tools_changed()
            return
        # HX offers the server no capabilities, so the only request it can
        # expect an answer to is `ping`. Anything else is refused rather than
        # left hanging until the server's own timeout.
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        if method == "ping":
            reply["result"] = {}
        else:
            reply["error"] = {"code": -32601, "message": f"method not found: {method}"}
        task = asyncio.create_task(self._reply(reply))
        self._replies.add(task)
        task.add_done_callback(self._replies.discard)

    async def _reply(self, reply: dict[str, Any]) -> None:
        try:
            await self.transport.send(reply)
        except Exception as exc:
            log.debug("mcp %s: could not answer the server: %s", self.name, exc)

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPError(f"{self.name}: {reason}"))

    async def close(self) -> None:
        for task in list(self._replies):
            task.cancel()
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


class MCPAuthRequired(MCPError):
    """The server wants a signed-in user, and there is no token it accepts."""


class MCPSessionExpired(MCPError):
    """The server no longer knows the session; ``initialize`` starts a new one."""
