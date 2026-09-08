"""Grep tool: content search.

Shells out to ``ripgrep`` when present and falls back to a pure-Python walker
otherwise, so the tool works on a machine without rg rather than failing.
"""

from __future__ import annotations

from typing import Any

from hx.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = """Search file contents with a regular expression.

Modes: `files_with_matches` (default), `content` (with -A/-B/-C context), or
`count`. Filter with `glob` or `type`."""


class GrepTool(Tool):
    name = "Grep"
    description = DESCRIPTION
    mutating = False

    def schema(self) -> dict[str, Any]:
        raise NotImplementedError

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError


def has_ripgrep() -> bool:
    raise NotImplementedError
