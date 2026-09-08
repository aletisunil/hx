"""Tool registry.

Holds builtins, MCP-backed tools and skill-provided tools in one place. The
ordering rule matters: schemas are rendered into the cached prefix, so the sort
must be stable across runs and across MCP server restarts.
"""

from __future__ import annotations

import asyncio
from typing import Any

from hx.tools.base import Tool, ToolContext, ToolError, ToolResult


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Raises :class:`DuplicateTool` on a name collision."""
        if tool.name in self._tools:
            raise DuplicateTool(tool.name)
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownTool(name) from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools, key=_sort_key)

    def schemas(self, allowed: set[str] | None = None) -> list[dict[str, Any]]:
        """Rendered schemas in canonical order: builtins first, then ``mcp__*``,
        each sorted by name.

        The order must not depend on registration order or MCP connection order -
        the serialised block sits in the cached prefix.

        Args:
            allowed: Restrict to this set - used for subagent tool allowlists
                and permission-mode restrictions (plan mode hides mutating tools).
        """
        names = [n for n in self.names() if allowed is None or n in allowed]
        return [
            {
                "name": name,
                "description": self._tools[name].description,
                "input_schema": self._tools[name].schema(),
            }
            for name in names
        ]

    async def call(self, name: str, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Validate, dispatch, and convert exceptions into error results.

        A failing tool returns an error result rather than raising, so the model
        sees what went wrong and can adapt instead of the turn dying.
        """
        try:
            tool = self.get(name)
        except UnknownTool:
            available = ", ".join(self.names()) or "none"
            return ToolResult(
                content=f"Unknown tool {name!r}. Available tools: {available}",
                is_error=True,
                summary="unknown tool",
            )

        try:
            validated = tool.validate(params)
            return await tool.run(validated, ctx)
        except ToolError as exc:
            return ToolResult(content=str(exc), is_error=True, summary="error")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolResult(
                content=f"{type(exc).__name__}: {exc}",
                is_error=True,
                summary="error",
            )


def _sort_key(name: str) -> tuple[bool, str]:
    return (name.startswith("mcp__"), name)


def build_default_registry() -> ToolRegistry:
    """Registry with the builtin tools registered in canonical order."""
    return ToolRegistry()


class DuplicateTool(Exception):
    pass


class UnknownTool(Exception):
    pass
