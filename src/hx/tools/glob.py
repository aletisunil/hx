"""Glob tool: fast path matching, sorted by modification time."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from hx.tools.base import Tool, ToolContext, ToolError, ToolResult

DESCRIPTION = """Find files by glob pattern (e.g. `**/*.py`, `src/**/*.ts`).

Returns paths sorted by modification time, newest first."""

IGNORED_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".ruff_cache", "dist"}
)
DEFAULT_LIMIT = 200


class GlobTool(Tool):
    name = "Glob"
    description = DESCRIPTION
    mutating = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Directory to search (default cwd)"},
                "limit": {"type": "integer", "default": DEFAULT_LIMIT},
            },
            "required": ["pattern"],
        }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # Filesystem work runs off the event loop: a slow disk or a huge
        # tree would otherwise freeze the TUI mid-render.
        return await asyncio.to_thread(self._run, params, ctx)

    def _run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = Path(params.get("path") or ctx.cwd).expanduser()
        if not root.is_absolute():
            root = ctx.cwd / root
        if not root.is_dir():
            raise ToolError(f"{root}: not a directory")

        limit = int(params.get("limit") or DEFAULT_LIMIT)
        try:
            matches = [
                path
                for path in root.glob(str(params["pattern"]))
                if path.is_file() and not _ignored(path)
            ]
        except (ValueError, OSError) as exc:
            raise ToolError(f"bad pattern: {exc}") from exc

        matches.sort(key=_mtime, reverse=True)
        shown = matches[:limit]
        body = "\n".join(str(path) for path in shown) or "(no matches)"
        if len(matches) > limit:
            body += f"\n\n... {len(matches) - limit} more matches (raise limit to see them)"
        return ToolResult(content=body, summary=f"{len(matches)} matches")


def _ignored(path: Path) -> bool:
    return any(part in IGNORED_DIRS for part in path.parts)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
