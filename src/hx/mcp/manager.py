"""MCP server lifecycle and tool registration.

Config lives in ``<cwd>/.hx/mcp.json`` and ``~/.hx/mcp.json``, project last.
Servers connect concurrently at startup with a per-server timeout; a failure
logs a warning and drops that server, never kills the session.

Tools are namespaced ``mcp__<server>__<tool>`` and registered into the same
:class:`~hx.tools.registry.ToolRegistry` as builtins, sorted deterministically -
a server whose tool order varies between runs would invalidate the cache prefix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hx.mcp.client import MCPClient
from hx.tools.base import Tool, ToolContext, ToolResult

NAMESPACE_TEMPLATE = "mcp__{server}__{tool}"


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

    def __init__(self, client: MCPClient, server: str, definition: Any) -> None:
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        raise NotImplementedError

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError


class MCPManager:
    def __init__(self, configs: list[MCPServerConfig]) -> None:
        raise NotImplementedError

    async def connect_all(self) -> list[ServerStatus]:
        """Connect concurrently. Never raises for a single server's failure."""
        raise NotImplementedError

    async def register_tools(self, registry: Any) -> None:
        raise NotImplementedError

    def status(self) -> list[ServerStatus]:
        """Backs ``/mcp`` and ``hx mcp list``."""
        raise NotImplementedError

    async def close_all(self) -> None:
        raise NotImplementedError


def load_configs(cwd: Path) -> list[MCPServerConfig]:
    """Read and merge user and project ``mcp.json``. Project entries win by name."""
    raise NotImplementedError


def save_config(config: MCPServerConfig, cwd: Path, user_level: bool = False) -> None:
    """Backs ``hx mcp add``."""
    raise NotImplementedError
