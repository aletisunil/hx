"""Write tool: create or fully replace a file."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from hx.tools.base import Tool, ToolContext, ToolError, ToolResult
from hx.tools.read import FileTracker, resolve_path

DESCRIPTION = """Write a file, overwriting it if it exists.

Overwriting a file that has not been read in this session is refused - use Read
first, or Edit for partial changes."""


class WriteTool(Tool):
    name = "Write"
    description = DESCRIPTION
    mutating = True

    def __init__(self, tracker: FileTracker) -> None:
        """Args:
        tracker: ``hx.tools.read.FileTracker``, used to enforce read-before-write.
        """
        self.tracker = tracker

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        }

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        return str(params.get("file_path", ""))

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Writes atomically (temp file + rename) so a crash cannot truncate the target."""
        # Filesystem work runs off the event loop: a slow disk or a huge
        # tree would otherwise freeze the TUI mid-render.
        return await asyncio.to_thread(self._run, params, ctx)

    def _run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_path(params["file_path"], ctx.cwd)
        content = str(params["content"])

        if path.exists():
            if path.is_dir():
                raise ToolError(f"{path} is a directory")
            if not self.tracker.was_read(path):
                raise ToolError(
                    f"{path} exists but has not been read in this session. "
                    "Read it first so the overwrite is deliberate."
                )
            if self.tracker.changed_since_read(path):
                raise ToolError(
                    f"{path} changed on disk since it was read. Read it again before writing."
                )

        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, content)
        self.tracker.mark_read(path)

        verb = "updated" if existed else "created"
        lines = content.count("\n") + 1 if content else 0
        return ToolResult(
            content=f"{verb} {path} ({lines} lines)",
            summary=f"{verb} {path.name}",
            metadata={"path": str(path), "created": not existed},
        )


def write_atomic(path: Any, content: str) -> None:
    """Write via a temp file in the same directory, then rename.

    Same directory matters: a rename across filesystems is not atomic, and the
    point of this is that a crash never leaves a half-written file.
    """
    tmp = path.with_name(f".{path.name}.hx-tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
