"""Per-tool presentation.

A tool call is the most common thing on screen, so it is worth drawing well.
``Bash(command='rg -n foo src')`` is a Python repr of a dict; ``$ rg -n foo
src`` is what the user actually ran. Every builtin gets a renderer that says
what happened in the vocabulary of the tool, and unknown tools - MCP servers,
extensions - fall back to a generic header rather than to nothing.

Renderers are pure: they take the call, its output and its metadata, and return
Rich renderables. They never touch the loop or a widget, so they can be
unit-tested without a running app.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.text import Text

from hx.tools.edit import count_changes as _count_changes
from hx.tui.theme import THEME

COLLAPSED_LINES = 6
"""Output lines kept in a collapsed block. Enough to see that something
happened and roughly what; Ctrl+R has the rest."""

BASH_COLLAPSED_LINES = 8
"""Command output earns a little more room: it is usually the answer itself,
not a preview of one."""


def expand_hint() -> str:
    """``ctrl+o to expand`` - the key comes from the registry, so a rebind moves it."""
    from hx.keys import primary_key

    return f"{primary_key('app.tools.expand')} to expand"


MAX_DIFF_LINES = 200
"""Diff lines shown in a permission prompt. Scrolling past 4000 lines to find
the Approve button is not review, it is fatigue."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Everything a renderer is allowed to know about one tool call."""

    name: str
    params: dict[str, Any]
    cwd: Path
    output: str = ""
    summary: str = ""
    metadata: dict[str, Any] | None = None
    is_error: bool = False
    finished: bool = False
    duration_ms: float = 0.0
    expanded: bool = False


def display_path(raw: Any, cwd: Path) -> str:
    """Path as a human would name it: relative to the project, else under ``~``.

    An absolute path inside a tool header is noise - forty characters of prefix
    the user already knows, pushing the part they do not know off the edge.
    """
    if not raw:
        return ""
    text = str(raw)
    try:
        path = Path(text).expanduser()
    except (OSError, ValueError):
        return text

    for base, prefix in ((cwd, ""), (Path.home(), "~/")):
        try:
            return prefix + str(path.resolve().relative_to(base.resolve()))
        except (OSError, ValueError):
            continue
    return text


def _lexer_for(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".pyi": "python",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".js": "javascript",
        ".jsx": "jsx",
        ".json": "json",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".md": "markdown",
        ".rs": "rust",
        ".go": "go",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".css": "css",
        ".tcss": "css",
        ".html": "html",
        ".sql": "sql",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".java": "java",
        ".rb": "ruby",
    }.get(suffix)


def _syntax_theme() -> Any:
    """Pygments style for the active palette, so code follows the theme."""
    from hx.tui.theme import syntax_style

    return syntax_style()


def highlight(code: str, path: str) -> RenderableType:
    """Syntax-highlight ``code`` when the file type is known, else dim text."""
    lexer = _lexer_for(path)
    if lexer is None:
        return Text(code, style=THEME.fg("tool_output"))
    return Syntax(
        code,
        lexer,
        theme=_syntax_theme(),
        background_color="default",
        word_wrap=False,
    )


def truncate_hint(hidden: int) -> Text:
    """``… (12 more lines, ctrl+r to expand)`` - pi's affordance, verbatim."""
    return Text.assemble(
        (f"… ({hidden} more line{'s' if hidden != 1 else ''}, ", THEME.fg("muted")),
        (expand_hint(), THEME.fg("dim")),
        (")", THEME.fg("muted")),
    )


def _tail(text: str, limit: int, expanded: bool) -> tuple[list[str], int]:
    """Last ``limit`` lines, plus how many were dropped."""
    lines = text.splitlines()
    if expanded or len(lines) <= limit:
        return lines, 0
    return lines[-limit:], len(lines) - limit


def _head(text: str, limit: int, expanded: bool) -> tuple[list[str], int]:
    lines = text.splitlines()
    if expanded or len(lines) <= limit:
        return lines, 0
    return lines[:limit], len(lines) - limit


def format_duration(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.1f}s"


# --------------------------------------------------------------------------- #
# Diffs
# --------------------------------------------------------------------------- #

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_WORD = re.compile(r"\s+|\w+|[^\w\s]")


def _word_diff(before: str, after: str) -> tuple[Text, Text]:
    """Highlight the words that actually changed within a modified line.

    A one-character change on a 100-character line is invisible when the whole
    line is painted red and green; inverting only the changed run points at it.
    """
    old_words = _WORD.findall(before)
    new_words = _WORD.findall(after)
    removed = Text(style=THEME.fg("diff_removed"))
    added = Text(style=THEME.fg("diff_added"))
    removed_mark = f"{THEME.color('diff_removed')} on {THEME.color('tool_error_bg')} bold"
    added_mark = f"{THEME.color('diff_added')} on {THEME.color('tool_success_bg')} bold"
    matcher = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        old_run = "".join(old_words[i1:i2])
        new_run = "".join(new_words[j1:j2])
        if op == "equal":
            removed.append(old_run)
            added.append(new_run)
            continue
        removed.append(old_run, style=removed_mark if old_run.strip() else "")
        added.append(new_run, style=added_mark if new_run.strip() else "")
    return removed, added


def render_diff(diff_text: str, *, max_lines: int = 200) -> RenderableType:
    """A unified diff, numbered and coloured the way pi draws one.

    Line numbers are carried from the hunk headers rather than shown as ``@@``
    noise, so a reviewer can map a change onto the file without counting.
    """
    body = Text(no_wrap=False)
    old_no = new_no = 0
    emitted = 0
    pending_removed: list[tuple[int, str]] = []
    pending_added: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal emitted
        if len(pending_removed) == 1 and len(pending_added) == 1:
            (old_line, old_text), (new_line, new_text) = pending_removed[0], pending_added[0]
            removed, added = _word_diff(old_text, new_text)
            body.append(f"-{old_line:>5} ", style=THEME.fg("diff_removed"))
            body.append_text(removed)
            body.append("\n")
            body.append(f"+{new_line:>5} ", style=THEME.fg("diff_added"))
            body.append_text(added)
            body.append("\n")
            emitted += 2
        else:
            for line_no, text in pending_removed:
                body.append(f"-{line_no:>5} {text}\n", style=THEME.fg("diff_removed"))
                emitted += 1
            for line_no, text in pending_added:
                body.append(f"+{line_no:>5} {text}\n", style=THEME.fg("diff_added"))
                emitted += 1
        pending_removed.clear()
        pending_added.clear()

    for line in diff_text.splitlines():
        # Pending lines have not been emitted yet but will be, so they count
        # against the cap. Without them a long run of additions never flushes
        # inside the loop, `emitted` stays at zero, and the whole diff renders.
        if emitted + len(pending_removed) + len(pending_added) >= max_lines:
            break
        if line.startswith(("+++", "---")):
            continue
        hunk = _HUNK.match(line)
        if hunk:
            flush()
            old_no, new_no = int(hunk.group(1)), int(hunk.group(3))
            if emitted:
                body.append("…\n", style=THEME.fg("diff_context"))
            continue
        if line.startswith("-"):
            pending_removed.append((old_no, line[1:].expandtabs(4)))
            old_no += 1
        elif line.startswith("+"):
            pending_added.append((new_no, line[1:].expandtabs(4)))
            new_no += 1
        else:
            flush()
            body.append(f" {new_no:>5} {line[1:].expandtabs(4)}\n", style=THEME.fg("diff_context"))
            old_no += 1
            new_no += 1
            emitted += 1
    flush()

    total = sum(
        1 for line in diff_text.splitlines() if line[:1] in "+- " and line[:3] not in ("+++", "---")
    )
    if total > emitted:
        body.append(f"… {total - emitted} more diff lines\n", style=THEME.fg("warning"))
    return body


# Owned by the tools layer, which produces the diffs. The TUI may depend on
# tools; the reverse would be a layering inversion, so it is imported, not copied.
count_changes = _count_changes


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #


class ToolRenderer:
    """Draws one tool. Subclasses override the two halves they care about."""

    verb: str = ""

    def header(self, call: ToolCall) -> Text:
        """The single line always on screen, collapsed or not."""
        return Text.assemble(
            (self.verb or call.name.lower(), THEME.fg("tool_title", bold=True)),
            (f" {_brief(call.params)}", THEME.fg("muted")),
        )

    def body(self, call: ToolCall) -> RenderableType | None:
        """What sits under the header. ``None`` draws nothing."""
        return _plain_output(call, COLLAPSED_LINES)


class ReadRenderer(ToolRenderer):
    verb = "read"

    def header(self, call: ToolCall) -> Text:
        path = display_path(call.params.get("file_path") or call.params.get("path"), call.cwd)
        header = Text.assemble(
            ("read ", THEME.fg("tool_title", bold=True)),
            (path, THEME.fg("accent")),
        )
        offset, limit = call.params.get("offset"), call.params.get("limit")
        if offset or limit:
            start = int(offset or 1)
            end = start + int(limit) - 1 if limit else ""
            header.append(f":{start}{f'-{end}' if end else ''}", style=THEME.fg("warning"))
        if call.finished and not call.is_error and call.summary:
            header.append(f"  {call.summary}", style=THEME.fg("dim"))
        return header

    def body(self, call: ToolCall) -> RenderableType | None:
        """Collapsed reads show nothing: the model read the file, the user did not.

        Expanding shows the window it actually saw, highlighted."""
        if not call.output or (not call.expanded and not call.is_error):
            return None
        if call.is_error:
            return Text(call.output.strip(), style=THEME.fg("error"))

        path = str(call.params.get("file_path") or call.params.get("path") or "")
        lines, hidden = _head(call.output, COLLAPSED_LINES, call.expanded)
        code = "\n".join(_strip_line_number(line) for line in lines)
        parts: list[RenderableType] = [highlight(code, path)]
        if hidden:
            parts.append(truncate_hint(hidden))
        return Group(*parts)


class WriteRenderer(ToolRenderer):
    verb = "write"

    def header(self, call: ToolCall) -> Text:
        path = display_path(call.params.get("file_path"), call.cwd)
        header = Text.assemble(
            ("write ", THEME.fg("tool_title", bold=True)),
            (path, THEME.fg("accent")),
        )
        content = str(call.params.get("content") or "")
        if content:
            count = len(content.splitlines())
            header.append(f"  {count} line{'s' if count != 1 else ''}", style=THEME.fg("dim"))
        return header

    def body(self, call: ToolCall) -> RenderableType | None:
        if call.is_error:
            return Text(call.output.strip(), style=THEME.fg("error"))
        content = str(call.params.get("content") or "")
        if not content:
            return None
        lines, hidden = _head(content, COLLAPSED_LINES, call.expanded)
        path = str(call.params.get("file_path") or "")
        parts: list[RenderableType] = [highlight("\n".join(lines), path)]
        if hidden:
            parts.append(truncate_hint(hidden))
        return Group(*parts)


class EditRenderer(ToolRenderer):
    verb = "edit"

    def header(self, call: ToolCall) -> Text:
        path = display_path(call.params.get("file_path"), call.cwd)
        header = Text.assemble(
            ("edit ", THEME.fg("tool_title", bold=True)),
            (path, THEME.fg("accent")),
        )
        diff = self._diff(call)
        if diff:
            added, removed = count_changes(diff)
            header.append(f"  +{added}", style=THEME.fg("diff_added"))
            header.append(f" -{removed}", style=THEME.fg("diff_removed"))
        return header

    @staticmethod
    def _diff(call: ToolCall) -> str:
        return str((call.metadata or {}).get("diff") or "")

    def body(self, call: ToolCall) -> RenderableType | None:
        """The diff is the whole point of an edit, so it is shown collapsed too -
        just clipped to a reviewable height."""
        if call.is_error:
            return Text(call.output.strip(), style=THEME.fg("error"))
        diff = self._diff(call)
        if not diff:
            return None
        return render_diff(diff, max_lines=400 if call.expanded else 16)


class BashRenderer(ToolRenderer):
    verb = "$"

    def header(self, call: ToolCall) -> Text:
        command = str(call.params.get("command") or "")
        header = Text.assemble(
            ("$ ", THEME.fg("accent", bold=True)),
            (command.strip() or "…", THEME.fg("tool_title", bold=True)),
        )
        if timeout := call.params.get("timeout"):
            header.append(f" (timeout {timeout}s)", style=THEME.fg("muted"))
        if call.params.get("run_in_background"):
            header.append("  background", style=THEME.fg("warning"))
        return header

    def body(self, call: ToolCall) -> RenderableType | None:
        parts: list[RenderableType] = []
        output = call.output.strip("\n")
        if output:
            lines, hidden = _tail(output, BASH_COLLAPSED_LINES, call.expanded)
            if hidden:
                parts.append(truncate_hint(hidden))
            style = THEME.fg("error") if call.is_error else THEME.fg("tool_output")
            parts.append(Text("\n".join(lines), style=style))
        if call.finished and call.duration_ms:
            label = "Took" if call.finished else "Elapsed"
            parts.append(
                Text(f"{label} {format_duration(call.duration_ms)}", style=THEME.fg("dim"))
            )
        return Group(*parts) if parts else None


class GrepRenderer(ToolRenderer):
    verb = "grep"

    def header(self, call: ToolCall) -> Text:
        header = Text.assemble(
            ("grep ", THEME.fg("tool_title", bold=True)),
            (str(call.params.get("pattern") or ""), THEME.fg("accent")),
        )
        if shown := _elsewhere(call.params.get("path"), call.cwd):
            header.append(f" in {shown}", style=THEME.fg("muted"))
        if glob := call.params.get("glob"):
            header.append(f" ({glob})", style=THEME.fg("muted"))
        if call.finished and call.summary:
            header.append(f"  {call.summary}", style=THEME.fg("dim"))
        return header


class GlobRenderer(ToolRenderer):
    verb = "glob"

    def header(self, call: ToolCall) -> Text:
        header = Text.assemble(
            ("glob ", THEME.fg("tool_title", bold=True)),
            (str(call.params.get("pattern") or ""), THEME.fg("accent")),
        )
        if shown := _elsewhere(call.params.get("path"), call.cwd):
            header.append(f" in {shown}", style=THEME.fg("muted"))
        if call.finished and call.summary:
            header.append(f"  {call.summary}", style=THEME.fg("dim"))
        return header


class TodoRenderer(ToolRenderer):
    verb = "todos"

    MARKERS: ClassVar[dict[str, str]] = {
        "completed": "✓",
        "in_progress": "▸",
        "pending": "○",
    }

    def header(self, call: ToolCall) -> Text:
        header = Text("todos", style=THEME.fg("tool_title", bold=True))
        if call.summary:
            header.append(f"  {call.summary}", style=THEME.fg("dim"))
        return header

    def body(self, call: ToolCall) -> RenderableType | None:
        """The list itself, not ``todos=[3 items]``. A plan is worth reading."""
        todos = call.params.get("todos")
        if not isinstance(todos, list) or not todos:
            return None
        body = Text()
        for todo in todos:
            if not isinstance(todo, dict):
                continue
            status = str(todo.get("status") or "pending")
            marker = self.MARKERS.get(status, "○")
            # active_form is optional; falling through to content keeps a todo
            # from rendering as the literal string "None".
            content = str(todo.get("content") or "")
            label = str(todo.get("active_form") or content) if status == "in_progress" else content
            style = {
                "completed": f"strike {THEME.fg('dim')}",
                "in_progress": THEME.fg("accent"),
            }.get(status, THEME.fg("muted"))
            marker_style = {
                "completed": THEME.fg("success"),
                "in_progress": THEME.fg("accent"),
            }.get(status, THEME.fg("dim"))
            body.append(f"{marker} ", style=marker_style)
            body.append(f"{label}\n", style=style)
        # Text.rstrip mutates and returns None; returning it directly would
        # draw no body at all.
        body.rstrip()
        return body


class TaskRenderer(ToolRenderer):
    verb = "task"

    def header(self, call: ToolCall) -> Text:
        header = Text.assemble(
            ("task ", THEME.fg("tool_title", bold=True)),
            (str(call.params.get("subagent_type") or "agent"), THEME.fg("accent")),
        )
        if description := call.params.get("description"):
            header.append(f"  {description}", style=THEME.fg("muted"))
        return header


RENDERERS: dict[str, ToolRenderer] = {
    "read": ReadRenderer(),
    "write": WriteRenderer(),
    "edit": EditRenderer(),
    "multiedit": EditRenderer(),
    "bash": BashRenderer(),
    "bashoutput": BashRenderer(),
    "grep": GrepRenderer(),
    "glob": GlobRenderer(),
    "todowrite": TodoRenderer(),
    "task": TaskRenderer(),
}
FALLBACK = ToolRenderer()


def renderer_for(name: str) -> ToolRenderer:
    """The renderer for ``name``, or the generic one for tools HX does not own."""
    return RENDERERS.get(name.lower(), FALLBACK)


def _elsewhere(path: Any, cwd: Path) -> str:
    """The path, unless it is the directory the session is already in.

    ``grep TODO in .`` says nothing the user did not already know."""
    shown = display_path(path, cwd)
    return "" if shown in ("", ".") else shown


def _strip_line_number(line: str) -> str:
    """Drop the ``   12\\t`` prefix the read tool adds, keeping the code."""
    head, tab, rest = line.partition("\t")
    return rest if tab and head.strip().isdigit() else line


def _plain_output(call: ToolCall, limit: int) -> RenderableType | None:
    if not call.output.strip():
        return None
    lines, hidden = _head(call.output, limit, call.expanded)
    style = THEME.fg("error") if call.is_error else THEME.fg("tool_output")
    body = Text("\n".join(lines), style=style)
    if hidden:
        return Group(body, truncate_hint(hidden))
    return body


def _brief(params: dict[str, Any], limit: int = 72) -> str:
    """Compact ``key=value`` preview for tools without a renderer of their own."""
    parts: list[str] = []
    for key, value in params.items():
        if isinstance(value, list):
            rendered = f"[{len(value)} items]"
        elif isinstance(value, dict):
            rendered = "{…}"
        elif isinstance(value, str) and len(value) > limit:
            rendered = repr(value[: limit - 1] + "…")
        else:
            rendered = repr(value)
        parts.append(f"{key}={rendered}")
    joined = ", ".join(parts)
    return joined if len(joined) <= limit else joined[: limit - 1] + "…"
