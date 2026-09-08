"""Read tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hx.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = """Read a file from the local filesystem.

Returns `cat -n` style numbered lines. Use `offset`/`limit` for large files.
Images are returned for viewing; notebooks are returned as cells."""

DEFAULT_LINE_LIMIT = 2000
MAX_LINE_LENGTH = 2000
"""Individual lines longer than this are truncated - a minified bundle on one
line would otherwise blow the window in a single read."""


class ReadTool(Tool):
    name = "Read"
    description = DESCRIPTION
    mutating = False

    def schema(self) -> dict[str, Any]:
        raise NotImplementedError

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        raise NotImplementedError

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError


class FileTracker:
    """Records which files were read and their mtime/hash at read time.

    Edit refuses to touch a file that was never read, and warns when a file
    changed underneath us. The set of stale files is surfaced through late
    injection, not the system prompt.
    """

    def __init__(self) -> None:
        raise NotImplementedError

    def mark_read(self, path: Path) -> None:
        raise NotImplementedError

    def was_read(self, path: Path) -> bool:
        raise NotImplementedError

    def changed_since_read(self, path: Path) -> bool:
        raise NotImplementedError

    def stale_files(self) -> list[Path]:
        raise NotImplementedError
