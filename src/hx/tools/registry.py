"""Tool registry.

Holds builtins, MCP-backed tools and skill-provided tools in one place. The
ordering rule matters: schemas are rendered into the cached prefix, so the sort
must be stable across runs and across MCP server restarts.
"""

from __future__ import annotations

import asyncio
from typing import Any

from hx.auth.store import TAVILY
from hx.tools.base import Tool, ToolContext, ToolError, ToolResult


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Raises :class:`DuplicateTool` on a name collision."""
        if tool.name in self._tools:
            raise DuplicateTool(tool.name)
        self._tools[tool.name] = tool

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

    def subset(self, names: set[str]) -> ToolRegistry:
        """A registry exposing only ``names``, sharing the same tool instances.

        Used for subagent allowlists. Sharing instances matters: the persistent
        shell and the file tracker must be the same objects, or a subagent
        would get a second shell with none of the session's state.
        """
        restricted = ToolRegistry()
        for name in names:
            if name in self._tools:
                restricted._tools[name] = self._tools[name]
        return restricted

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


def build_default_registry(
    shell: Any | None = None,
    jobs: Any | None = None,
    tracker: Any | None = None,
    todos: Any | None = None,
    bus: Any | None = None,
    auth: Any | None = None,
    checkpoints: Any | None = None,
) -> ToolRegistry:
    """Registry with the builtin tools registered.

    Bash is registered only when a shell is supplied, so a headless caller that
    does not want command execution simply does not pass one. The web tools
    follow the same rule against ``auth``: without a Tavily key they are left
    out entirely rather than advertised and failing, because their schemas sit
    in the cached prefix and a model told "no key" simply tries again.
    """
    from hx.tools.bash import BashOutputTool, BashTool, KillShellTool
    from hx.tools.edit import EditTool
    from hx.tools.glob import GlobTool
    from hx.tools.grep import GrepTool
    from hx.tools.read import FileTracker, ReadTool
    from hx.tools.todo import TodoWriteTool
    from hx.tools.write import WriteTool

    file_tracker = tracker if tracker is not None else FileTracker()

    registry = ToolRegistry()
    if shell is not None and jobs is not None:
        registry.register(BashTool(shell, jobs))
        registry.register(BashOutputTool(jobs))
        registry.register(KillShellTool(jobs))
    registry.register(ReadTool(file_tracker))
    registry.register(WriteTool(file_tracker, checkpoints))
    registry.register(EditTool(file_tracker, checkpoints))
    registry.register(GlobTool())
    registry.register(GrepTool())
    if todos is not None:
        registry.register(TodoWriteTool(todos, bus))
    if auth is not None and auth.has_credential(TAVILY):
        from hx.tools.websearch import CreditLedger, WebFetchTool, WebSearchTool

        # One ledger between them: the bill is per key, not per tool.
        ledger = CreditLedger()
        registry.register(WebSearchTool(auth, ledger))
        registry.register(WebFetchTool(auth, ledger))
    return registry


class DuplicateTool(Exception):
    pass


class UnknownTool(Exception):
    pass
