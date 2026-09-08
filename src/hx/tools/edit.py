"""Edit tool: exact string replacement.

Exact-match replacement rather than diff application: the model either matched
the file or it did not, and an ambiguous match is an error instead of a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hx.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = """Replace an exact string in a file.

`old_string` must appear exactly once unless `replace_all` is set. Read the
file first. For multiple edits to one file, pass a list of edits."""


@dataclass(slots=True)
class EditOp:
    old_string: str
    new_string: str
    replace_all: bool = False


class EditTool(Tool):
    name = "Edit"
    description = DESCRIPTION
    mutating = True

    def __init__(self, tracker: Any) -> None:
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        raise NotImplementedError

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        raise NotImplementedError

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Applies every edit or none - a partial application would leave the file
        in a state neither the model nor the user asked for."""
        raise NotImplementedError

    def apply(self, content: str, edits: list[EditOp]) -> str:
        """Pure function over file content. Raises :class:`~hx.tools.base.ToolError`
        when a match is missing or ambiguous."""
        raise NotImplementedError


def unified_diff(before: str, after: str, path: str) -> str:
    """Diff for the TUI edit block and the permission prompt - the user sees the
    exact change before approving it."""
    raise NotImplementedError
