"""Edit tool: exact string replacement.

Exact-match replacement rather than diff application: the model either matched
the file or it did not, and an ambiguous match is an error instead of a guess.
"""

from __future__ import annotations

import asyncio
import difflib
from dataclasses import dataclass
from typing import Any

from hx.tools.base import Tool, ToolContext, ToolError, ToolResult
from hx.tools.read import FileTracker, resolve_path
from hx.tools.write import write_atomic

DESCRIPTION = """Replace an exact string in a file.

`old_string` must appear exactly once unless `replace_all` is set. Read the
file first. Pass `edits` to apply several replacements to one file atomically."""


@dataclass(slots=True)
class EditOp:
    old_string: str
    new_string: str
    replace_all: bool = False


class EditTool(Tool):
    name = "Edit"
    description = DESCRIPTION
    mutating = True

    def __init__(self, tracker: FileTracker) -> None:
        self.tracker = tracker

    def schema(self) -> dict[str, Any]:
        edit_properties = {
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
        }
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean", "default": False},
                "edits": {
                    "type": "array",
                    "description": "Several edits applied to one file, in order",
                    "items": {
                        "type": "object",
                        "properties": edit_properties,
                        "required": ["old_string", "new_string"],
                    },
                },
            },
            "required": ["file_path"],
        }

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        return str(params.get("file_path", ""))

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Applies every edit or none - a partial application would leave the file
        in a state neither the model nor the user asked for."""
        # Filesystem work runs off the event loop: a slow disk or a huge
        # tree would otherwise freeze the TUI mid-render.
        return await asyncio.to_thread(self._run, params, ctx)

    def _run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_path(params["file_path"], ctx.cwd)
        edits = parse_edits(params)

        if not path.is_file():
            raise ToolError(f"{path}: no such file")
        if not self.tracker.was_read(path):
            raise ToolError(f"{path} has not been read in this session. Read it first.")
        if self.tracker.changed_since_read(path):
            raise ToolError(f"{path} changed on disk since it was read. Read it again.")

        before = path.read_text(encoding="utf-8")
        after = self.apply(before, edits)

        write_atomic(path, after)
        self.tracker.mark_read(path)

        diff = unified_diff(before, after, str(path))
        added, removed = count_changes(diff)
        return ToolResult(
            content=f"Applied {len(edits)} edit(s) to {path}.\n\n{diff}",
            summary=f"{path.name} +{added} -{removed}",
            metadata={"path": str(path), "diff": diff},
        )

    def apply(self, content: str, edits: list[EditOp]) -> str:
        """Pure function over file content. Raises :class:`ToolError`
        when a match is missing or ambiguous."""
        result = content
        for index, edit in enumerate(edits, start=1):
            if edit.old_string == edit.new_string:
                raise ToolError(f"edit {index}: old_string and new_string are identical")

            occurrences = result.count(edit.old_string)
            if occurrences == 0:
                raise ToolError(
                    f"edit {index}: old_string not found in the file. "
                    "It must match exactly, including whitespace and indentation."
                )
            if occurrences > 1 and not edit.replace_all:
                raise ToolError(
                    f"edit {index}: old_string appears {occurrences} times. "
                    "Add surrounding context to make it unique, or set replace_all."
                )
            result = result.replace(
                edit.old_string,
                edit.new_string,
                -1 if edit.replace_all else 1,
            )
        return result


def parse_edits(params: dict[str, Any]) -> list[EditOp]:
    raw = params.get("edits")
    if raw:
        return [
            EditOp(
                old_string=str(item["old_string"]),
                new_string=str(item["new_string"]),
                replace_all=bool(item.get("replace_all", False)),
            )
            for item in raw
        ]
    if "old_string" not in params or "new_string" not in params:
        raise ToolError("Edit needs either old_string/new_string or a list of edits")
    return [
        EditOp(
            old_string=str(params["old_string"]),
            new_string=str(params["new_string"]),
            replace_all=bool(params.get("replace_all", False)),
        )
    ]


def count_changes(diff_text: str) -> tuple[int, int]:
    """Added and removed line counts, ignoring the ``+++``/``---`` file headers."""
    added = sum(1 for line in diff_text.splitlines() if line.startswith("+") and line[1:2] != "+")
    removed = sum(1 for line in diff_text.splitlines() if line.startswith("-") and line[1:2] != "-")
    return added, removed


def unified_diff(before: str, after: str, path: str) -> str:
    """Diff for the TUI edit block and the permission prompt - the user sees the
    exact change before approving it."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
            n=3,
        )
    )
