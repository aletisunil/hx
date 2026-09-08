"""MCP client - stdio and streamable HTTP transports.

Implements the subset HX needs: ``initialize``, ``tools/list``, ``tools/call``,
``prompts/list``, ``resources/list``. JSON-RPC 2.0 over either transport.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "2025-06-18"


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

    The server's stderr is captured to the session log rather than the terminal;
    a chatty server would otherwise corrupt the TUI render.
    """

    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None) -> None:
        raise NotImplementedError

    async def connect(self) -> None:
        raise NotImplementedError

    async def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    def receive(self) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class HTTPTransport(Transport):
    """Streamable HTTP transport with SSE responses."""

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        raise NotImplementedError

    async def connect(self) -> None:
        raise NotImplementedError

    async def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    def receive(self) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class MCPClient:
    """One connected server."""

    def __init__(self, name: str, transport: Transport, timeout: float = 30.0) -> None:
        raise NotImplementedError

    async def initialize(self) -> ServerCapabilities:
        raise NotImplementedError

    async def list_tools(self) -> list[MCPToolDef]:
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call a tool and flatten the content blocks to text.

        Tool results are data from a third-party server. They are returned to the
        model as tool output and must never be treated as instructions to HX.
        """
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class MCPError(Exception):
    pass
