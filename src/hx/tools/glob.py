"""Glob tool: fast path matching, sorted by modification time."""

from __future__ import annotations

from typing import Any

from hx.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = """Find files by glob pattern (e.g. `**/*.py`, `src/**/*.ts`).

Returns paths sorted by modification time, newest first. Respects .gitignore."""


class GlobTool(Tool):
    name = "Glob"
    description = DESCRIPTION
    mutating = False

    def schema(self) -> dict[str, Any]:
        raise NotImplementedError

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError
