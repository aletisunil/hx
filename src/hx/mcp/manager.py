"""MCP server lifecycle and tool registration.

Config lives in ``<cwd>/.hx/mcp.json`` and ``~/.hx/mcp.json``, project last.
Servers connect concurrently at startup with a per-server timeout; a failure
logs a warning and drops that server, never kills the session.

Tools are namespaced ``mcp__<server>__<tool>`` and registered into the same
:class:`~hx.tools.registry.ToolRegistry` as builtins, sorted deterministically -
a server whose tool order varies between runs would invalidate the cache prefix.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hx.mcp.client import MCPClient, MCPError, MCPToolDef, StdioTransport
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


@dataclass(slots=True)
class ServerStatus:
    name: str
    connected: bool
    tool_count: int
    error: str | None = None


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
            output = await self.client.call_tool(self.definition.name, params)
        except MCPError as exc:
            raise ToolError(str(exc)) from exc

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
        )


class MCPManager:
    def __init__(self, configs: list[MCPServerConfig]) -> None:
        self.configs = [config for config in configs if config.enabled]
        self.clients: dict[str, MCPClient] = {}
        self._status: dict[str, ServerStatus] = {}
        self._tools: dict[str, list[MCPToolDef]] = {}

    async def connect_all(self) -> list[ServerStatus]:
        """Connect concurrently. Never raises for a single server's failure."""
        results = await asyncio.gather(
            *(self._connect(config) for config in self.configs),
            return_exceptions=True,
        )
        for config, result in zip(self.configs, results, strict=True):
            if isinstance(result, BaseException):
                self._status[config.name] = ServerStatus(config.name, False, 0, str(result))
                log.warning("mcp server %s failed to start: %s", config.name, result)
        return self.status()

    async def _connect(self, config: MCPServerConfig) -> None:
        client = MCPClient(config.name, _build_transport(config), timeout=config.timeout)
        try:
            await asyncio.wait_for(client.initialize(), timeout=config.timeout)
            definitions = await client.list_tools()
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

        self.clients[config.name] = client
        self._tools[config.name] = definitions
        self._status[config.name] = ServerStatus(config.name, True, len(definitions))

    async def register_tools(self, registry: Any) -> None:
        """Register every connected server's tools under its namespace.

        Servers are visited in sorted order and their tools sorted by name, so
        the schema block is byte-identical between runs even if a server
        returns its tools in a different order.
        """
        for server in sorted(self.clients):
            client = self.clients[server]
            for definition in sorted(self._tools[server], key=lambda item: item.name):
                tool = MCPTool(client, server, definition)
                if registry.has(tool.name):
                    log.warning("mcp tool %s already registered; skipping", tool.name)
                    continue
                registry.register(tool)

    def status(self) -> list[ServerStatus]:
        """Backs ``/mcp`` and ``hx mcp list``."""
        return [
            self._status.get(config.name, ServerStatus(config.name, False, 0, "not started"))
            for config in self.configs
        ]

    async def close_all(self) -> None:
        await asyncio.gather(
            *(client.close() for client in self.clients.values()), return_exceptions=True
        )
        self.clients.clear()


def _build_transport(config: MCPServerConfig) -> Any:
    if config.transport == "stdio":
        if not config.command:
            raise MCPError(f"{config.name}: stdio transport needs a command")
        return StdioTransport(config.command, list(config.args), config.env)
    if config.transport in {"http", "sse"}:
        from hx.mcp.client import HTTPTransport

        if not config.url:
            raise MCPError(f"{config.name}: http transport needs a url")
        return HTTPTransport(config.url, config.headers)
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
