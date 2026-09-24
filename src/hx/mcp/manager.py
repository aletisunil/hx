"""MCP server lifecycle and tool registration.

Config lives in ``<cwd>/.hx/mcp.json`` and ``~/.hx/mcp.json``, project last.
Servers connect concurrently at startup with a per-server timeout; a failure
logs a warning and drops that server, never kills the session.

Tools are namespaced ``mcp__<server>__<tool>`` and registered into the same
:class:`~hx.tools.registry.ToolRegistry` as builtins, sorted deterministically -
a server whose tool order varies between runs would invalidate the cache prefix.
A server that announces a changed tool list has its tools swapped in place.

A remote server that wants a signed-in user is not an error: it is reported as
needing sign-in, and :meth:`MCPManager.login` runs the browser flow and brings
it up without a restart. Nothing opens a browser at startup on its own.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hx.auth.oauth.browser import LoginInteraction
from hx.auth.store import AuthStore
from hx.core.images import ImageError, load_image
from hx.core.messages import ImageBlock
from hx.mcp import oauth
from hx.mcp.client import MCPAuthRequired, MCPClient, MCPError, MCPToolDef, StdioTransport
from hx.mcp.oauth import OAuthOptions
from hx.tools.base import Tool, ToolContext, ToolError, ToolResult
from hx.tools.output import cap_output, summarize_for_ui

NAMESPACE_TEMPLATE = "mcp__{server}__{tool}"

log = logging.getLogger(__name__)


@dataclass(slots=True)
class MCPServerConfig:
    name: str
    transport: str
    """``stdio`` or ``http``."""
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: float = 30.0
    oauth: OAuthOptions = field(default_factory=OAuthOptions)

    @property
    def uses_oauth(self) -> bool:
        """An HTTP server whose config does not already say how to authenticate.

        An ``Authorization`` header in ``mcp.json`` is the user's choice - an
        API token, say - and OAuth stays out of its way.
        """
        return self.transport in HTTP_TRANSPORTS and not any(
            key.lower() == "authorization" for key in self.headers
        )


HTTP_TRANSPORTS = frozenset({"http", "sse"})

NEEDS_SIGN_IN = "needs sign-in"


@dataclass(slots=True)
class ServerStatus:
    name: str
    connected: bool
    tool_count: int
    error: str | None = None
    needs_login: bool = False
    """The server refused for want of a token: sign-in, not a fix, is the remedy."""


class MCPTool(Tool):
    """Adapter presenting an MCP tool through the normal :class:`Tool` interface."""

    #: MCP servers are third-party code; assume a call can have side effects
    #: unless the schema says otherwise. Concurrency and prompting follow from
    #: this, so guessing "read-only" would be the unsafe guess.
    mutating = True

    def __init__(self, client: MCPClient, server: str, definition: MCPToolDef) -> None:
        self.client = client
        self.server = server
        self.definition = definition
        self.name = NAMESPACE_TEMPLATE.format(server=server, tool=definition.name)
        self.description = definition.description or f"{definition.name} (from {server})"

    def schema(self) -> dict[str, Any]:
        return self.definition.input_schema

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        return self.definition.name

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = await self.client.call_tool(self.definition.name, params)
        except MCPError as exc:
            raise ToolError(str(exc)) from exc

        output = result.text
        # Off the event loop: a full-page screenshot takes seconds to decode
        # and shrink, and the session would freeze for all of them.
        images, problems = await asyncio.to_thread(_images, result.images, self.definition.name)
        if problems:
            output = "\n".join([output, *problems])
        capped = cap_output(
            output,
            session_id=ctx.session_id,
            tool_use_id=ctx.tool_use_id,
            char_cap=ctx.settings.context.tool_output_char_cap,
            line_cap=ctx.settings.context.tool_output_line_cap,
        )
        return ToolResult(
            content=capped.text,
            spilled_path=capped.spilled_path,
            summary=summarize_for_ui(output),
            images=images,
        )


def _images(raw: list[tuple[str, str]], tool: str) -> tuple[list[ImageBlock], list[str]]:
    """Normalise a server's images, and say which could not be.

    A server's image is whatever size it chose - a full-page browser screenshot
    is easily past what a route accepts - so each goes through the same
    normalisation as a pasted one. One that fails becomes a line of text, so the
    model knows an image was returned rather than seeing nothing.
    """
    images: list[ImageBlock] = []
    problems: list[str] = []
    for index, (data, _mime) in enumerate(raw, start=1):
        label = f"{tool} image {index}" if len(raw) > 1 else f"{tool} image"
        try:
            images.append(load_image(base64.b64decode(data, validate=False), label=label))
        except (ImageError, binascii.Error) as exc:
            problems.append(f"[{label} could not be shown: {exc}]")
    return images, problems


class MCPManager:
    def __init__(self, configs: list[MCPServerConfig], store: AuthStore | None = None) -> None:
        self.configs = [config for config in configs if config.enabled]
        self.clients: dict[str, MCPClient] = {}
        self.store = store or AuthStore()
        self._status: dict[str, ServerStatus] = {}
        self._tools: dict[str, list[MCPToolDef]] = {}
        self._registry: Any = None
        self._registered: dict[str, list[str]] = {}
        """Per server, the registry names its tools went in under."""
        self._refreshing: dict[str, asyncio.Task[None]] = {}
        self._refresh_again: set[str] = set()

    def config(self, name: str) -> MCPServerConfig | None:
        return next((config for config in self.configs if config.name == name), None)

    async def connect_all(self) -> list[ServerStatus]:
        """Connect concurrently. Never raises for a single server's failure."""
        await asyncio.gather(*(self._connect_or_report(config) for config in self.configs))
        return self.status()

    async def _connect_or_report(self, config: MCPServerConfig) -> None:
        """:meth:`_connect`, with anything it did not expect recorded as the
        server's status rather than raised - at startup and on ``/mcp
        reconnect`` alike."""
        try:
            await self._connect(config)
        except Exception as exc:
            self._status[config.name] = ServerStatus(
                config.name, False, 0, str(exc) or type(exc).__name__
            )
            log.warning("mcp server %s failed to start: %s", config.name, exc)

    async def _connect(self, config: MCPServerConfig) -> None:
        client = MCPClient(
            config.name,
            _build_transport(config, self.store),
            timeout=config.timeout,
            on_tools_changed=lambda: self._tools_changed(config.name),
        )
        try:
            await asyncio.wait_for(client.initialize(), timeout=config.timeout)
            definitions = await client.list_tools()
        except MCPAuthRequired as exc:
            await client.close()
            self._status[config.name] = ServerStatus(
                config.name, False, 0, str(exc) or NEEDS_SIGN_IN, needs_login=True
            )
            log.info("mcp server %s %s", config.name, NEEDS_SIGN_IN)
            return
        except (TimeoutError, MCPError, OSError) as exc:
            await client.close()
            # One bad server must not cost the user their whole session.
            # asyncio.TimeoutError stringifies to nothing, so say what happened.
            reason = (
                f"did not respond within {config.timeout:.0f}s"
                if isinstance(exc, TimeoutError)
                else str(exc) or type(exc).__name__
            )
            self._status[config.name] = ServerStatus(config.name, False, 0, reason)
            log.warning("mcp server %s unavailable: %s", config.name, reason)
            return
        except BaseException:
            # Not ours to report - but the process it started is ours to stop.
            await client.close()
            raise

        self.clients[config.name] = client
        self._tools[config.name] = definitions
        self._status[config.name] = ServerStatus(config.name, True, len(definitions))

    async def register_tools(self, registry: Any) -> None:
        """Register every connected server's tools under its namespace.

        Servers are visited in sorted order and their tools sorted by name, so
        the schema block is byte-identical between runs even if a server
        returns its tools in a different order. The registry is kept, so a
        server that signs in or changes its tools later lands in it too.
        """
        self._registry = registry
        for server in sorted(self.clients):
            self._register_server(server)

    def _register_server(self, server: str) -> None:
        registry = self._registry
        client = self.clients.get(server)
        if registry is None or client is None:
            return
        names: list[str] = []
        for definition in sorted(self._tools.get(server, []), key=lambda item: item.name):
            tool = MCPTool(client, server, definition)
            if registry.has(tool.name):
                log.warning("mcp tool %s already registered; skipping", tool.name)
                continue
            registry.register(tool)
            names.append(tool.name)
        self._registered[server] = names

    def _unregister_server(self, server: str) -> None:
        if self._registry is None:
            return
        for name in self._registered.pop(server, []):
            self._registry.unregister(name)

    def _tools_changed(self, server: str) -> None:
        """Re-list a server's tools after it said they changed.

        Coalesced: a server that announces twice while the first re-list is in
        flight gets exactly one more, not one per announcement.
        """
        running = self._refreshing.get(server)
        if running is not None and not running.done():
            self._refresh_again.add(server)
            return
        self._refreshing[server] = asyncio.create_task(self._relist(server))

    async def _relist(self, server: str) -> None:
        while True:
            self._refresh_again.discard(server)
            client = self.clients.get(server)
            if client is None:
                return
            try:
                definitions = await client.list_tools()
            except MCPError as exc:
                log.warning("mcp server %s: could not re-list tools: %s", server, exc)
                return
            self._unregister_server(server)
            self._tools[server] = definitions
            self._register_server(server)
            self._status[server] = ServerStatus(server, True, len(definitions))
            log.info("mcp server %s now offers %d tools", server, len(definitions))
            if server not in self._refresh_again:
                return

    async def reconnect(self, name: str) -> ServerStatus:
        """Drop one server's connection and tools, and connect it again."""
        config = self.config(name)
        if config is None:
            raise MCPError(f"no MCP server named {name!r}")
        await self._disconnect(name)
        await self._connect_or_report(config)
        self._register_server(name)
        return self._status[name]

    async def login(self, name: str, interaction: LoginInteraction) -> ServerStatus:
        """Sign in to one server in the browser, save the token, reconnect.

        Raises:
            MCPError: for an unknown server, or one that has no sign-in.
            OAuthError / CallbackError: when the sign-in itself fails.
        """
        config = self.config(name)
        if config is None:
            raise MCPError(f"no MCP server named {name!r}")
        await sign_in(config, interaction, self.store)
        return await self.reconnect(name)

    async def logout(self, name: str) -> bool:
        """Forget one server's token, and reconnect it without. Whether one
        was stored.

        A server with nothing stored - one that sends its own header, or asks
        for no sign-in at all - is left as it is. One that had a token is asked
        again rather than assumed to need one: whether it does is the server's
        answer to give.
        """
        config = self.config(name)
        if config is None or not config.url or not config.uses_oauth:
            return False
        removed = await asyncio.to_thread(self.store.delete, oauth.credential_id(config.url))
        if removed:
            await self.reconnect(name)
        return removed

    async def _disconnect(self, name: str) -> None:
        refresh = self._refreshing.pop(name, None)
        if refresh is not None:
            refresh.cancel()
        self._unregister_server(name)
        self._tools.pop(name, None)
        client = self.clients.pop(name, None)
        if client is not None:
            await client.close()

    def status(self) -> list[ServerStatus]:
        """Backs ``/mcp`` and ``hx mcp list``."""
        return [
            self._status.get(config.name, ServerStatus(config.name, False, 0, "not started"))
            for config in self.configs
        ]

    async def close_all(self) -> None:
        for task in self._refreshing.values():
            task.cancel()
        self._refreshing.clear()
        await asyncio.gather(
            *(client.close() for client in self.clients.values()), return_exceptions=True
        )
        self.clients.clear()


async def sign_in(
    config: MCPServerConfig, interaction: LoginInteraction, store: AuthStore | None = None
) -> None:
    """Run the browser sign-in for ``config`` and save the token.

    Raises:
        MCPError: when the server is not one OAuth applies to.
        OAuthError / CallbackError: when the sign-in itself fails.
    """
    if not config.url or config.transport not in HTTP_TRANSPORTS:
        raise MCPError(f"{config.name} is a local server; there is nothing to sign in to.")
    if not config.uses_oauth:
        raise MCPError(
            f"{config.name} sends its own Authorization header from mcp.json; "
            "remove it to sign in with OAuth instead."
        )
    store = store or AuthStore()
    credential = await oauth.login(config.url, interaction, config.oauth, label=config.name)
    await asyncio.to_thread(store.save, oauth.credential_id(config.url), credential)


def _build_transport(config: MCPServerConfig, store: AuthStore | None = None) -> Any:
    if config.transport == "stdio":
        if not config.command:
            raise MCPError(f"{config.name}: stdio transport needs a command")
        return StdioTransport(config.command, list(config.args), config.env)
    if config.transport in HTTP_TRANSPORTS:
        from hx.mcp.client import HTTPTransport

        if not config.url:
            raise MCPError(f"{config.name}: http transport needs a url")
        auth = oauth.OAuthSession(config.url, store or AuthStore()) if config.uses_oauth else None
        return HTTPTransport(config.url, config.headers, auth)
    raise MCPError(f"{config.name}: unknown transport {config.transport!r}")


def load_configs(cwd: Path) -> list[MCPServerConfig]:
    """Read and merge user and project ``mcp.json``. Project entries win by name."""
    from hx.paths import project_mcp_file, user_home

    merged: dict[str, MCPServerConfig] = {}
    for path in (user_home() / "mcp.json", project_mcp_file(cwd)):
        for config in _read_file(path):
            merged[config.name] = config
    return [merged[name] for name in sorted(merged)]


def _read_file(path: Path) -> list[MCPServerConfig]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("ignoring %s: %s", path, exc)
        return []

    servers = data.get("mcpServers") or data.get("servers") or {}
    configs: list[MCPServerConfig] = []
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            continue
        transport = str(raw.get("type") or ("http" if raw.get("url") else "stdio"))
        configs.append(
            MCPServerConfig(
                name=str(name),
                transport=transport,
                command=raw.get("command"),
                args=tuple(str(arg) for arg in raw.get("args") or ()),
                env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
                url=raw.get("url"),
                headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
                enabled=bool(raw.get("enabled", True)),
                timeout=float(raw.get("timeout", 30.0)),
                oauth=OAuthOptions.from_json(raw.get("oauth")),
            )
        )
    return configs


def save_config(config: MCPServerConfig, cwd: Path, user_level: bool = False) -> Path:
    """Backs ``hx mcp add``."""
    from hx.paths import project_mcp_file, user_home

    path = (user_home() / "mcp.json") if user_level else project_mcp_file(cwd)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}

    servers = data.setdefault("mcpServers", {})
    entry: dict[str, Any] = {"type": config.transport}
    if config.command:
        entry["command"] = config.command
    if config.args:
        entry["args"] = list(config.args)
    if config.env:
        entry["env"] = config.env
    if config.url:
        entry["url"] = config.url
    if config.headers:
        entry["headers"] = dict(config.headers)
    if oauth_block := config.oauth.to_json():
        entry["oauth"] = oauth_block
    servers[config.name] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return path


def remove_config(name: str, cwd: Path, user_level: bool = False) -> bool:
    from hx.paths import project_mcp_file, user_home

    path = (user_home() / "mcp.json") if user_level else project_mcp_file(cwd)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False

    servers = data.get("mcpServers") or {}
    if name not in servers:
        return False
    del servers[name]
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return True
