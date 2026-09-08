"""Grep tool: content search.

Shells out to ``ripgrep`` when present and falls back to a pure-Python walker
otherwise, so the tool works on a machine without rg rather than failing.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hx.tools.base import Tool, ToolContext, ToolError, ToolResult
from hx.tools.glob import IGNORED_DIRS

DESCRIPTION = """Search file contents with a regular expression.

Modes: `files_with_matches` (default), `content` (with -A/-B/-C context), or
`count`. Filter with `glob`. Case-insensitive with `-i`."""

DEFAULT_LIMIT = 200
SEARCH_TIMEOUT = 60.0


class GrepTool(Tool):
    name = "Grep"
    description = DESCRIPTION
    mutating = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression"},
                "path": {"type": "string", "description": "File or directory (default cwd)"},
                "glob": {"type": "string", "description": "Filter files, e.g. '*.py'"},
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                    "default": "files_with_matches",
                },
                "-i": {"type": "boolean", "description": "Case insensitive"},
                "-A": {"type": "integer", "description": "Lines of trailing context"},
                "-B": {"type": "integer", "description": "Lines of leading context"},
                "-C": {"type": "integer", "description": "Lines of context either side"},
                "limit": {"type": "integer", "default": DEFAULT_LIMIT},
            },
            "required": ["pattern"],
        }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return await asyncio.to_thread(self._run, params, ctx)

    def _run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(params["pattern"])
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc

        root = Path(params.get("path") or ctx.cwd).expanduser()
        if not root.is_absolute():
            root = ctx.cwd / root
        if not root.exists():
            raise ToolError(f"{root}: no such file or directory")

        mode = str(params.get("output_mode") or "files_with_matches")
        limit = int(params.get("limit") or DEFAULT_LIMIT)

        if has_ripgrep():
            lines = self._ripgrep(pattern, root, mode, params)
        else:
            lines = self._python_search(pattern, root, mode, params)

        shown = lines[:limit]
        body = "\n".join(shown) or "(no matches)"
        if len(lines) > limit:
            body += f"\n\n... {len(lines) - limit} more results (raise limit to see them)"
        return ToolResult(content=body, summary=f"{len(lines)} results")

    def _ripgrep(self, pattern: str, root: Path, mode: str, params: dict[str, Any]) -> list[str]:
        argv = ["rg", "--no-heading", "--color", "never"]
        if mode == "files_with_matches":
            argv.append("--files-with-matches")
        elif mode == "count":
            argv.append("--count")
        else:
            argv.append("--line-number")
            for flag, key in (("-A", "-A"), ("-B", "-B"), ("-C", "-C")):
                if params.get(key) is not None:
                    argv += [flag, str(int(params[key]))]
        if params.get("-i"):
            argv.append("--ignore-case")
        if glob := params.get("glob"):
            argv += ["--glob", str(glob)]
        argv += ["--regexp", pattern, str(root)]

        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=SEARCH_TIMEOUT, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"search timed out after {SEARCH_TIMEOUT:.0f}s") from exc

        # rg exits 1 for "no matches", which is not an error condition here.
        if result.returncode not in (0, 1):
            raise ToolError(result.stderr.strip() or f"ripgrep exited {result.returncode}")
        return [line for line in result.stdout.splitlines() if line]

    def _python_search(
        self, pattern: str, root: Path, mode: str, params: dict[str, Any]
    ) -> list[str]:
        flags = re.IGNORECASE if params.get("-i") else 0
        regex = re.compile(pattern, flags)
        glob_filter = str(params.get("glob") or "")
        context = int(params.get("-C") or 0)
        before = int(params.get("-B") or context)
        after = int(params.get("-A") or context)

        results: list[str] = []
        for path in _walk(root):
            if glob_filter and not path.match(glob_filter):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            hits = [index for index, line in enumerate(lines) if regex.search(line)]
            if not hits:
                continue

            if mode == "files_with_matches":
                results.append(str(path))
            elif mode == "count":
                results.append(f"{path}:{len(hits)}")
            else:
                results.extend(_render_context(path, lines, hits, before, after))
        return results


def _walk(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and not any(part in IGNORED_DIRS for part in path.parts)
    ]


def _render_context(
    path: Path, lines: list[str], hits: list[int], before: int, after: int
) -> list[str]:
    wanted: set[int] = set()
    for hit in hits:
        wanted.update(range(max(0, hit - before), min(len(lines), hit + after + 1)))
    return [f"{path}:{index + 1}:{lines[index]}" for index in sorted(wanted)]


def has_ripgrep() -> bool:
    return shutil.which("rg") is not None
