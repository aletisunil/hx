"""Edit tool: exact string replacement, or replacement by content anchor.

Exact-match replacement rather than diff application: the model either matched
the file or it did not, and an ambiguous match is an error instead of a guess.

``hashline`` is the second shape. Instead of retyping the lines it wants gone,
the model names the anchors ``Read`` printed beside them and supplies only the
replacement. Anchors are recomputed from disk at edit time, so a file that moved
under the model fails to resolve rather than being patched at stale coordinates.
See :mod:`hx.tools.anchors`.
"""

from __future__ import annotations

import asyncio
import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hx.tools import anchors
from hx.tools.base import Tool, ToolContext, ToolError, ToolResult
from hx.tools.read import FileTracker, hashline_enabled, resolve_path
from hx.tools.write import write_atomic

if TYPE_CHECKING:
    from hx.core.checkpoints import CheckpointStore

DESCRIPTION = """Replace part of a file. Read the file first.

Two ways to say what to replace:

- `old_string`/`new_string` - exact match. `old_string` must appear exactly
  once unless `replace_all` is set. Pass `edits` for several of these applied
  to one file atomically.
- `hashline` - the anchors Read printed beside each line. `{"start": "a3f9",
  "end": "b7c2", "new_string": "..."}` replaces lines a3f9 through b7c2
  inclusive. Omit `end` to replace one line, and pass an empty `new_string` to
  delete the span. Cheaper than retyping the block, and a stale anchor is
  rejected instead of applied to the wrong lines.

Either shape, not both."""


@dataclass(slots=True)
class EditOp:
    old_string: str
    new_string: str
    replace_all: bool = False


@dataclass(slots=True)
class HashlineOp:
    start: str
    end: str
    new_string: str


class EditTool(Tool):
    name = "Edit"
    description = DESCRIPTION
    mutating = True

    def __init__(self, tracker: FileTracker, checkpoints: CheckpointStore | None = None) -> None:
        self.tracker = tracker
        self.checkpoints = checkpoints
        """See :class:`hx.tools.write.WriteTool` - the pre-image ``/rewind`` restores."""

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
                "hashline": {
                    "type": "array",
                    "description": "Line spans named by the anchors Read printed",
                    "items": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string", "description": "First line's anchor"},
                            "end": {
                                "type": "string",
                                "description": "Last line's anchor. Defaults to start.",
                            },
                            "new_string": {"type": "string"},
                        },
                        "required": ["start", "new_string"],
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

        if not path.is_file():
            raise ToolError(f"{path}: no such file")
        if not self.tracker.was_read(path):
            raise ToolError(f"{path} has not been read in this session. Read it first.")
        if self.tracker.changed_since_read(path):
            raise ToolError(f"{path} changed on disk since it was read. Read it again.")

        before = path.read_text(encoding="utf-8")

        if params.get("hashline"):
            if not hashline_enabled(ctx):
                raise ToolError("hashline edits are disabled (tools.hashline is false)")
            if params.get("edits") or "old_string" in params:
                raise ToolError(
                    "Edit takes hashline or old_string/edits, not both. Applying one and "
                    "dropping the other would silently lose half of what was asked for."
                )
            spans = parse_hashline(params)
            after = apply_hashline(before, spans)
            count = len(spans)
        else:
            edits = parse_edits(params)
            after = self.apply(before, edits)
            count = len(edits)

        if self.checkpoints is not None:
            self.checkpoints.capture(path)
        write_atomic(path, after)
        self.tracker.mark_read(path)
        if self.checkpoints is not None:
            self.checkpoints.settle(path)

        diff = unified_diff(before, after, str(path))
        added, removed = count_changes(diff)
        return ToolResult(
            content=f"Applied {count} edit(s) to {path}.\n\n{diff}",
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


def apply_hashline(content: str, spans: list[HashlineOp]) -> str:
    """Resolve each span against the current content and replace it.

    Spans are applied in the order given, each against the result of the last -
    anchors name content, not coordinates, so an earlier replacement only
    disturbs a later anchor when the two spans touch. That case surfaces as an
    unresolvable anchor, which is the honest answer.
    """
    lines, trailing_newline = anchors.split(content)
    # Replacement text arrives with bare newlines. In a CRLF file that would
    # leave the edited lines as the only LF ones in it, which is a diff nobody
    # asked for.
    crlf = sum(line.endswith("\r") for line in lines) * 2 > len(lines)

    for index, span in enumerate(spans, start=1):
        try:
            start = anchors.resolve(lines, span.start)
            end = anchors.resolve(lines, span.end) if span.end != span.start else start
        except anchors.AnchorError as exc:
            raise ToolError(f"hashline {index}: {exc}") from exc

        if end < start:
            raise ToolError(
                f"hashline {index}: end anchor {span.end!r} is at line {end + 1}, "
                f"before start anchor {span.start!r} at line {start + 1}"
            )
        lines = anchors.replace_span(lines, start, end, _terminated(span.new_string, crlf))

    result = "\n".join(lines)
    return result + "\n" if trailing_newline and result else result


def _terminated(replacement: str, crlf: bool) -> str:
    """Give the replacement the line endings the rest of the file uses."""
    if not crlf or not replacement:
        return replacement
    return "\n".join(
        part if part.endswith("\r") else f"{part}\r" for part in replacement.split("\n")
    )


def parse_hashline(params: dict[str, Any]) -> list[HashlineOp]:
    raw = params.get("hashline") or []
    spans: list[HashlineOp] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ToolError(f"each hashline entry must be an object, got {type(item).__name__}")
        if "start" not in item or "new_string" not in item:
            raise ToolError("each hashline entry needs start and new_string")
        start = str(item["start"]).strip().lstrip("#")
        spans.append(
            HashlineOp(
                start=start,
                end=str(item.get("end") or start).strip().lstrip("#"),
                new_string=str(item["new_string"]),
            )
        )
    if not spans:
        raise ToolError("hashline was empty")
    return spans


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
