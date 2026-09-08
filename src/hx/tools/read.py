"""Read tool."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from hx.tools.base import Tool, ToolContext, ToolError, ToolResult

DESCRIPTION = """Read a file from the local filesystem.

Returns `cat -n` style numbered lines. Use `offset`/`limit` for large files.
Prefer reading the part you need over reading a whole large file."""

DEFAULT_LINE_LIMIT = 2000
MAX_LINE_LENGTH = 2000
"""Individual lines longer than this are truncated - a minified bundle on one
line would otherwise blow the window in a single read."""

BINARY_SNIFF_BYTES = 8192


class ReadTool(Tool):
    name = "Read"
    description = DESCRIPTION
    mutating = False

    def __init__(self, tracker: FileTracker) -> None:
        self.tracker = tracker

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute or cwd-relative path"},
                "offset": {"type": "integer", "description": "First line to read (1-based)"},
                "limit": {"type": "integer", "description": "How many lines to read"},
            },
            "required": ["file_path"],
        }

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        return str(params.get("file_path", ""))

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # Filesystem work runs off the event loop: a slow disk or a huge
        # tree would otherwise freeze the TUI mid-render.
        return await asyncio.to_thread(self._run, params, ctx)

    def _run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_path(params["file_path"], ctx.cwd)
        if not path.exists():
            raise ToolError(f"{path}: no such file")
        if path.is_dir():
            raise ToolError(f"{path} is a directory. Use Glob to list its contents.")
        if is_binary(path):
            size = path.stat().st_size
            raise ToolError(f"{path}: binary file ({size} bytes), not read as text")

        offset = max(int(params.get("offset") or 1), 1)
        limit = int(params.get("limit") or DEFAULT_LINE_LIMIT)

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"{path}: {exc}") from exc

        lines = content.splitlines()
        window = lines[offset - 1 : offset - 1 + limit]
        rendered = "\n".join(
            f"{number:>6}\t{_clip(line)}" for number, line in enumerate(window, start=offset)
        )

        self.tracker.mark_read(path)

        remaining = len(lines) - (offset - 1 + len(window))
        if remaining > 0:
            rendered += (
                f"\n\n... {remaining} more lines. Re-read with offset={offset + len(window)}."
            )

        if not window:
            rendered = "(empty file)" if not lines else f"(no lines at offset {offset})"

        return ToolResult(content=rendered, summary=f"read {len(window)} lines")


class FileTracker:
    """Records which files were read and their content hash at read time.

    Edit refuses to touch a file that was never read, and warns when a file
    changed underneath us. The set of stale files is surfaced through late
    injection, not the system prompt.
    """

    def __init__(self) -> None:
        self._hashes: dict[Path, str] = {}

    def mark_read(self, path: Path) -> None:
        self._hashes[path.resolve()] = _digest(path)

    def was_read(self, path: Path) -> bool:
        return path.resolve() in self._hashes

    def changed_since_read(self, path: Path) -> bool:
        resolved = path.resolve()
        known = self._hashes.get(resolved)
        return known is not None and known != _digest(path)

    def stale_files(self) -> list[Path]:
        return [path for path in self._hashes if self.changed_since_read(path)]


def resolve_path(raw: str, cwd: Path) -> Path:
    """Resolve a model-supplied path against the session cwd.

    Relative paths are common in model output even when the tool asks for
    absolute ones, so they are accepted rather than rejected.
    """
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (cwd / path)


def is_binary(path: Path) -> bool:
    try:
        chunk = path.open("rb").read(BINARY_SNIFF_BYTES)
    except OSError:
        return False
    return b"\x00" in chunk


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_LENGTH else line[:MAX_LINE_LENGTH] + " … [line truncated]"
