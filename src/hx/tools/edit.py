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
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from hx.text import count
from hx.tools import anchors
from hx.tools.base import Tool, ToolContext, ToolError, ToolResult
from hx.tools.read import hashline_enabled, read_exact, resolve_path
from hx.tools.write import READ_FIRST, through_link, write_atomic

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

    def __init__(self, checkpoints: CheckpointStore | None = None) -> None:
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
        if not ctx.reads.was_read(path):
            raise ToolError(f"{path} has not been read in this session. {READ_FIRST}")
        if ctx.reads.changed_since_read(path):
            raise ToolError(f"{path} changed on disk since it was read. Read it again.")

        try:
            before = read_exact(path)
        except UnicodeDecodeError as exc:
            raise ToolError(
                f"{path} is not UTF-8 text ({exc.reason} at byte {exc.start}). "
                "Edit only changes UTF-8 files."
            ) from exc

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
            applied = len(spans)
        else:
            edits = parse_edits(params)
            after = apply_edits(before, edits)
            applied = len(edits)

        if self.checkpoints is not None:
            self.checkpoints.capture(path)
        write_atomic(path, after)
        ctx.reads.mark_read(path)
        if self.checkpoints is not None:
            self.checkpoints.settle(path)

        diff = unified_diff(before, after, str(path))
        added, removed = count_changes(diff)
        return ToolResult(
            content=f"Applied {count(applied, 'edit')} to {path}{through_link(path)}.\n\n{diff}",
            summary=f"{path.name} +{added} -{removed}",
            metadata={"path": str(path), "diff": diff},
        )


def apply_edits(content: str, edits: list[EditOp]) -> str:
    """Pure function over file content. Raises :class:`ToolError`
    when a match is missing or ambiguous.

    Shared with the approval preview, so the diff a user approves is computed
    exactly the way the edit will be applied.

    In a CRLF file the text arrives with bare newlines - Read shows lines
    without their carriage returns - so a multi-line ``old_string`` is matched
    in its CRLF spelling when the literal one is absent, and ``new_string``
    is given CRLF endings either way. Otherwise the edited lines would be the
    only LF ones in the file.
    """
    crlf = _uses_crlf(anchors.split(content)[0])
    result = content
    for index, edit in enumerate(edits, start=1):
        if edit.old_string == edit.new_string:
            raise ToolError(f"edit {index}: old_string and new_string are identical")

        old, new = edit.old_string, edit.new_string
        if crlf:
            new = _crlf(new)
            if result.count(old) == 0:
                old = _crlf(old)
            # Compared again once both are spelled the file's way: an edit that
            # only changed line endings came out identical here, replaced
            # nothing, and was still reported as applied.
            if old == new:
                raise ToolError(
                    f"edit {index}: old_string and new_string are identical once their "
                    "line endings follow the file's CRLF. Edit keeps a file's line endings."
                )

        occurrences = result.count(old)
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
        result = result.replace(old, new, -1 if edit.replace_all else 1)
    return result


def _uses_crlf(lines: list[str]) -> bool:
    """Whether most of a file's lines end in CRLF - the ending an edit should write."""
    return sum(line.endswith("\r") for line in lines) * 2 > len(lines)


def _crlf(text: str) -> str:
    """``text`` with every line ending spelled CRLF."""
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


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
    crlf = _uses_crlf(lines)

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
        lines = anchors.replace_span(lines, start, end, _replacement_lines(span.new_string, crlf))

    result = "\n".join(lines)
    return result + "\n" if trailing_newline and result else result


def _replacement_lines(replacement: str, crlf: bool) -> list[str]:
    """The lines a span becomes, ending the way the rest of the file's lines do.

    A newline at the end of the text closes its last line rather than opening
    one more: models write ``"TWO\n"`` for "the line becomes TWO", and splitting
    that literally added a blank line after every such edit. A lone newline is
    still one blank line, and empty text still deletes the span.
    """
    if not replacement:
        return []
    parts = replacement.replace("\r\n", "\n").split("\n")
    if len(parts) > 1 and parts[-1] == "":
        parts.pop()
    return [f"{part}\r" for part in parts] if crlf else parts


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


HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True, slots=True)
class DiffRow:
    """One meaningful line of a unified diff."""

    kind: Literal["file", "hunk", "line"]
    text: str
    """The file's name, the hunk header, or the line with its ``-``/``+``/space."""
    header: re.Match[str] | None = None
    """For a hunk: its parsed header, which numbers the lines that follow."""


def diff_rows(diff_text: str) -> Iterator[DiffRow]:
    """The files, hunks and changed or context lines of a unified diff.

    A hunk ends where its header says, after its count of old and new lines -
    not at the next line that looks like a header. A changed line can start
    with ``--`` or ``++`` (a SQL comment, ``++i``), so telling headers apart by
    their sign hid those lines; and a hunk that never ended counted the next
    file's ``---``/``+++`` headers as one removed and one added line.

    Everything between hunks other than a file's header pair - ``diff --git``,
    ``index``, mode lines - is left out.
    """
    lines = diff_text.splitlines()
    old_left = new_left = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if header := HUNK_HEADER.match(line):
            old_left = int(header.group(2)) if header.group(2) is not None else 1
            new_left = int(header.group(4)) if header.group(4) is not None else 1
            yield DiffRow("hunk", line, header)
        elif old_left > 0 or new_left > 0:
            if line.startswith("\\"):
                continue  # "\ No newline at end of file" belongs to no side
            if line.startswith("-"):
                old_left -= 1
            elif line.startswith("+"):
                new_left -= 1
            else:
                old_left -= 1
                new_left -= 1
            yield DiffRow("line", line)
        elif line.startswith("--- ") and index < len(lines) and lines[index].startswith("+++ "):
            yield DiffRow("file", _file_name(line[4:], lines[index][4:]))
            index += 1


def _file_name(old: str, new: str) -> str:
    """The path a ``---``/``+++`` pair is about: the new one, or the old one for a
    deletion, without git's ``a/``/``b/`` prefixes or a trailing timestamp."""
    old, new = old.split("\t", 1)[0], new.split("\t", 1)[0]
    sides = ((old, "a/"), (new, "b/"))
    if all(side.startswith(prefix) or side == "/dev/null" for side, prefix in sides):
        old, new = (side if side == "/dev/null" else side[2:] for side, _ in sides)
    return old if new == "/dev/null" else new


def count_changes(diff_text: str) -> tuple[int, int]:
    """Added and removed line counts, ignoring the ``+++``/``---`` file headers."""
    added = removed = 0
    for row in diff_rows(diff_text):
        if row.kind == "line" and row.text.startswith("+"):
            added += 1
        elif row.kind == "line" and row.text.startswith("-"):
            removed += 1
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
