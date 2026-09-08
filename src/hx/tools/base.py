"""Tool interface.

A tool declares a JSON schema, a permission specifier, and whether it mutates
state. The loop uses ``mutating`` to decide what may run concurrently, and the
permission engine uses ``permission_specifier`` to match rules like
``Bash(git commit:*)`` or ``Edit(src/**)``.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ToolContext:
    """Everything a tool is allowed to reach. Tools never touch global state."""

    cwd: Path
    session_id: str
    tool_use_id: str
    settings: Any
    """``hx.config.Settings`` - untyped here to avoid an import cycle."""
    emit_progress: Callable[[str], None]
    """Stream live output to the TUI. Not subject to context capping."""


@dataclass(slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    spilled_path: str | None = None
    summary: str = ""
    """One-line description for the collapsed TUI block, e.g. ``Read 340 lines``."""
    metadata: dict[str, Any] = field(default_factory=dict)


class Tool(abc.ABC):
    """Base class for every tool, builtin or MCP-backed."""

    name: str
    description: str
    mutating: bool = False
    """Mutating tools run serially and require permission in non-bypass modes."""

    @abc.abstractmethod
    def schema(self) -> dict[str, Any]:
        """JSON Schema for the tool input.

        Must be deterministic - the serialised schema block sits in the cached
        prefix, so a dict that iterates in a different order costs a cache miss.
        """

    @abc.abstractmethod
    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Execute. Raise :class:`ToolError` for user-facing failures."""

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        """The specifier matched against permission rules, e.g. the command
        string for Bash or the target path for Edit. ``None`` means the tool
        name alone is matched."""
        return None

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Check input against :meth:`schema` and drop unknown keys.

        Deliberately shallow: it catches the common failure (a missing required
        argument) without pulling in a JSON Schema engine. Tools still validate
        their own semantics.
        """
        schema = self.schema()
        properties = schema.get("properties") or {}
        missing = [key for key in schema.get("required", []) if key not in params]
        if missing:
            raise ToolError(f"{self.name}: missing required argument(s): {', '.join(missing)}")
        return {k: v for k, v in params.items() if k in properties} if properties else dict(params)


class StreamingTool(Tool):
    """Tool that produces incremental output (Bash, long-running scripts)."""

    @abc.abstractmethod
    def stream(self, params: dict[str, Any], ctx: ToolContext) -> AsyncIterator[str]: ...


class ToolError(Exception):
    """Failure that should be reported to the model as a tool result, not crash the turn."""
