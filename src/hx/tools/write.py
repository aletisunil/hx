"""Write tool: create or fully replace a file."""

from __future__ import annotations

from typing import Any

from hx.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = """Write a file, overwriting it if it exists.

Overwriting a file that has not been read in this session is refused - use Read
first, or Edit for partial changes."""


class WriteTool(Tool):
    name = "Write"
    description = DESCRIPTION
    mutating = True

    def __init__(self, tracker: Any) -> None:
        """Args:
        tracker: ``hx.tools.read.FileTracker``, used to enforce read-before-write.
        """
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        raise NotImplementedError

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        raise NotImplementedError

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Writes atomically (temp file + rename) so a crash cannot truncate the target."""
        raise NotImplementedError
