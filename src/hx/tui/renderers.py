"""Per-tool presentation.

A tool call is the most common thing on screen, so it is worth drawing well.
``Bash(command='rg -n foo src')`` is a Python repr of a dict; ``$ rg -n foo
src`` is what the user actually ran. Every builtin gets a renderer that says
what happened in the vocabulary of the tool, and unknown tools - MCP servers,
extensions - fall back to a generic header rather than to nothing.

Renderers are pure: they take the call, its output and its metadata, and return
strings. They never touch the loop or a view, so they can be unit-tested
without a running app - and, since they now return the text rather than a
renderable, tested by reading it.

The same renderer draws a call in the transcript and the detail of an approval
for that call. That is deliberate: the approval used to build its own version
of a command, without the ``$`` the tool block gave it, so the user agreed to
one shape of a thing and then watched a differently-shaped one run.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from hx.term.ansi import fg as paint_fg
from hx.term.sanitize import plain_text
from hx.term.syntax import highlight as highlight_source
from hx.term.width import cell_width, truncate_to_width
from hx.tools.edit import count_changes as _count_changes
from hx.tui.glyphs import BASH, ELLIPSIS, TODO_ACTIVE, TODO_DONE, TODO_PENDING
from hx.tui.limits import EXPANDED_MAX, PREVIEW_LINES, RECORD_WIDTH
from hx.tui.paint import color_mode, fg

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_WORD = re.compile(r"\s+|\w+|[^\w\s]")


def expand_note(hidden: int) -> str:
    """``… (12 more lines, ctrl+o to expand)``.

    One wording, one key, one place. There used to be two spellings of this -
    the transcript's and the approval prompt's - with different punctuation and
    different keys, for the same idea.
    """
    from hx.keys import primary_key

    plural = "s" if hidden != 1 else ""
    return (
        fg("muted", f"{ELLIPSIS} ({hidden} more line{plural}, ")
        + fg("dim", primary_key("app.tools.expand"))
        + fg("muted", " to expand)")
    )


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
    expanded: bool = False
    duration_ms: float = 0.0


#: Fields of a :class:`ToolCall` carrying text somebody else wrote.
SANITIZED_FIELDS = frozenset({"output", "summary", "params"})


def sanitized_fields(changes: dict[str, Any]) -> dict[str, Any]:
    """The subset of ``changes`` that has to be cleaned, cleaned.

    Split out from :func:`sanitized_call` so a caller replacing one field pays
    for that field, rather than for re-scanning everything already on the call.
    """
    out = dict(changes)
    for name in SANITIZED_FIELDS & changes.keys():
        value = changes[name]
        if name == "params":
            out[name] = {
                key: plain_text(item) if isinstance(item, str) else item
                for key, item in (value or {}).items()
            }
        elif isinstance(value, str):
            out[name] = plain_text(value)
    return out


def sanitized_call(call: ToolCall) -> ToolCall:
    """A call whose strings are safe to draw.

    Output, summary and the parameters alike: a tool's own arguments are as
    likely to carry an escape as its result, because a path or a command is
    often something the model copied out of a file it had read.

    Lives beside :class:`ToolCall` because both the transcript block and the
    approval prompt build one, and a renderer that is safe in one place and
    not the other is a renderer nobody can reason about.
    """
    from dataclasses import replace

    return replace(
        call,
        **sanitized_fields({"output": call.output, "summary": call.summary, "params": call.params}),
    )


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


_LEXERS = {
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
}


def lexer_for(path: str) -> str | None:
    return _LEXERS.get(Path(path).suffix.lower())


def highlight(code: str, path: str) -> list[str]:
    """Syntax-highlight ``code`` when the file type is known, else dim text."""
    language = lexer_for(path)
    if language is None:
        return [fg("tool_output", line) for line in code.split("\n")]
    return highlight_source(code, language, mode=color_mode())


def format_duration(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.1f}s"


def _tail(text: str, limit: int, expanded: bool) -> tuple[list[str], int]:
    """Last ``limit`` lines, plus how many were dropped."""
    lines = text.splitlines()
    if expanded:
        return lines[-EXPANDED_MAX:], max(0, len(lines) - EXPANDED_MAX)
    if len(lines) <= limit:
        return lines, 0
    return lines[-limit:], len(lines) - limit


def _head(text: str, limit: int, expanded: bool) -> tuple[list[str], int]:
    lines = text.splitlines()
    if expanded:
        return lines[:EXPANDED_MAX], max(0, len(lines) - EXPANDED_MAX)
    if len(lines) <= limit:
        return lines, 0
    return lines[:limit], len(lines) - limit


# --------------------------------------------------------------------------- #
# Diffs
# --------------------------------------------------------------------------- #


def _word_diff(before: str, after: str) -> tuple[str, str]:
    """Highlight the words that actually changed within a modified line.

    A one-character change on a 100-character line is invisible when the whole
    line is painted red and green; inverting only the changed run points at it.
    Inversion is reserved for this and for the text cursor, so that seeing it
    always means the same thing.
    """
    old_words = _WORD.findall(before)
    new_words = _WORD.findall(after)
    removed: list[str] = []
    added: list[str] = []

    mode = color_mode()

    def plain(text: str, role: str) -> str:
        return paint_fg(_c(role), text, mode)

    def mark(text: str, fg_role: str, bg_role: str) -> str:
        from hx.term.ansi import bg as paint_bg

        return paint_bg(_c(bg_role), paint_fg(_c(fg_role), text, mode, bold=True), mode)

    matcher = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        old_run = "".join(old_words[i1:i2])
        new_run = "".join(new_words[j1:j2])
        if op == "equal":
            removed.append(plain(old_run, "diff_removed"))
            added.append(plain(new_run, "diff_added"))
            continue
        removed.append(
            mark(old_run, "diff_removed", "tool_error_bg")
            if old_run.strip()
            else plain(old_run, "diff_removed")
        )
        added.append(
            mark(new_run, "diff_added", "tool_success_bg")
            if new_run.strip()
            else plain(new_run, "diff_added")
        )
    return "".join(removed), "".join(added)


def _c(role: str) -> str:
    from hx.tui.theme import THEME

    return THEME.color(role)


def render_diff(diff_text: str) -> list[str]:
    """A unified diff, numbered and coloured the way pi draws one.

    Line numbers are carried from the hunk headers rather than shown as ``@@``
    noise, so a reviewer can map a change onto the file without counting.

    The whole diff is rendered. Clipping belongs to whoever is showing it,
    against one limit, on the lines that actually came out - this used to take
    a cap of its own as well, so a diff inside an approval was clipped twice
    against two unrelated numbers.
    """
    out: list[str] = []
    old_no = new_no = 0
    pending_removed: list[tuple[int, str]] = []
    pending_added: list[tuple[int, str]] = []

    def flush() -> None:
        if len(pending_removed) == 1 and len(pending_added) == 1:
            (old_line, old_text), (new_line, new_text) = pending_removed[0], pending_added[0]
            removed, added = _word_diff(old_text, new_text)
            out.append(fg("diff_removed", f"-{old_line:>5} ") + removed)
            out.append(fg("diff_added", f"+{new_line:>5} ") + added)
        else:
            for line_no, text in pending_removed:
                out.append(fg("diff_removed", f"-{line_no:>5} {text}"))
            for line_no, text in pending_added:
                out.append(fg("diff_added", f"+{line_no:>5} {text}"))
        pending_removed.clear()
        pending_added.clear()

    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        hunk = _HUNK.match(line)
        if hunk:
            flush()
            old_no, new_no = int(hunk.group(1)), int(hunk.group(3))
            if out:
                out.append(fg("diff_context", ELLIPSIS))
            continue
        if line.startswith("-"):
            pending_removed.append((old_no, line[1:].expandtabs(4)))
            old_no += 1
        elif line.startswith("+"):
            pending_added.append((new_no, line[1:].expandtabs(4)))
            new_no += 1
        else:
            flush()
            out.append(fg("diff_context", f" {new_no:>5} {line[1:].expandtabs(4)}"))
            old_no += 1
            new_no += 1
    flush()
    return out


def looks_like_diff(text: str) -> bool:
    """Whether ``text`` is a unified diff.

    Requires a real hunk header, not just a leading ``---``. Sniffing on the
    prefix alone meant YAML front matter and a markdown rule were run through
    the diff painter, which strips exactly those lines and emits nothing.
    """
    return any(_HUNK.match(line) for line in text.splitlines())


#: Owned by the tools layer, which produces the diffs. The TUI may depend on
#: tools; the reverse would be a layering inversion, so it is imported.
count_changes = _count_changes


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #


class ToolRenderer:
    """Draws one tool. Subclasses override the parts they care about."""

    verb: str = ""

    preview_lines: int = PREVIEW_LINES
    """How much output survives collapsing.

    A declared exception rather than a magic number at a call site: a renderer
    that genuinely needs more room says so here, where it can be found.
    """

    def header(self, call: ToolCall) -> str:
        """The single line always on screen, collapsed or not."""
        return fg("tool_title", self.verb or call.name.lower(), bold=True) + fg(
            "muted", f" {_brief(call.params)}"
        )

    def body(self, call: ToolCall) -> list[str]:
        """What sits under the header."""
        return _plain_output(call, self.preview_lines)

    def detail(self, call: ToolCall) -> list[str]:
        """What an approval shows when asked to permit this call.

        Defaults to the header, so a tool with no special handling still gets
        described in its own vocabulary rather than as a dict.
        """
        return [self.header(call)]


class ReadRenderer(ToolRenderer):
    verb = "read"

    def header(self, call: ToolCall) -> str:
        path = display_path(call.params.get("file_path") or call.params.get("path"), call.cwd)
        header = fg("tool_title", "read ", bold=True) + fg("accent", path)
        offset, limit = call.params.get("offset"), call.params.get("limit")
        if offset or limit:
            start = int(offset or 1)
            end = start + int(limit) - 1 if limit else ""
            header += fg("warning", f":{start}{f'-{end}' if end else ''}")
        if call.finished and not call.is_error and call.summary:
            header += fg("dim", f"  {call.summary}")
        return header

    def body(self, call: ToolCall) -> list[str]:
        """Collapsed reads show nothing: the model read the file, the user did not.

        Expanding shows the window it actually saw, highlighted."""
        if not call.output or (not call.expanded and not call.is_error):
            return []
        if call.is_error:
            return [fg("error", line) for line in call.output.strip().split("\n")]

        path = str(call.params.get("file_path") or call.params.get("path") or "")
        lines, hidden = _head(call.output, self.preview_lines, call.expanded)
        code = "\n".join(_strip_line_number(line) for line in lines)
        out = highlight(code, path)
        return [*out, expand_note(hidden)] if hidden else out


class WriteRenderer(ToolRenderer):
    verb = "write"

    def header(self, call: ToolCall) -> str:
        path = display_path(call.params.get("file_path"), call.cwd)
        header = fg("tool_title", "write ", bold=True) + fg("accent", path)
        content = str(call.params.get("content") or "")
        if content:
            count = len(content.splitlines())
            header += fg("dim", f"  {count} line{'s' if count != 1 else ''}")
        return header

    def body(self, call: ToolCall) -> list[str]:
        if call.is_error:
            return [fg("error", line) for line in call.output.strip().split("\n")]
        content = str(call.params.get("content") or "")
        if not content:
            return []
        lines, hidden = _head(content, self.preview_lines, call.expanded)
        path = str(call.params.get("file_path") or "")
        out = highlight("\n".join(lines), path)
        return [*out, expand_note(hidden)] if hidden else out

    def detail(self, call: ToolCall) -> list[str]:
        return [self.header(call), *self.body(call)]


class EditRenderer(ToolRenderer):
    verb = "edit"

    def header(self, call: ToolCall) -> str:
        path = display_path(call.params.get("file_path"), call.cwd)
        header = fg("tool_title", "edit ", bold=True) + fg("accent", path)
        diff = self._diff(call)
        if diff:
            added, removed = count_changes(diff)
            header += fg("diff_added", f"  +{added}") + fg("diff_removed", f" -{removed}")
        return header

    @staticmethod
    def _diff(call: ToolCall) -> str:
        return str((call.metadata or {}).get("diff") or "")

    def body(self, call: ToolCall) -> list[str]:
        """The diff is the whole point of an edit, so it is shown collapsed too -
        just clipped to a reviewable height."""
        if call.is_error:
            return [fg("error", line) for line in call.output.strip().split("\n")]
        diff = self._diff(call)
        if not diff:
            return []
        return clip(render_diff(diff), self.preview_lines, call.expanded)

    def detail(self, call: ToolCall) -> list[str]:
        diff = self._diff(call)
        if not diff:
            return [self.header(call)]
        return [self.header(call), *render_diff(diff)]


class BashRenderer(ToolRenderer):
    verb = "$"

    def header(self, call: ToolCall) -> str:
        command = str(call.params.get("command") or "")
        header = fg("accent", BASH, bold=True) + fg(
            "tool_title", command.strip() or ELLIPSIS, bold=True
        )
        if timeout := call.params.get("timeout"):
            header += fg("muted", f" (timeout {timeout}s)")
        if call.params.get("run_in_background"):
            header += fg("warning", "  background")
        return header

    def body(self, call: ToolCall) -> list[str]:
        """The tail, not the head: a command's answer is at the end of it."""
        out: list[str] = []
        output = call.output.strip("\n")
        if output:
            lines, hidden = _tail(output, self.preview_lines, call.expanded)
            if hidden:
                out.append(expand_note(hidden))
            role = "error" if call.is_error else "tool_output"
            out.extend(fg(role, line) for line in lines)
        if call.finished and call.duration_ms:
            out.append(fg("dim", f"Took {format_duration(call.duration_ms)}"))
        return out

    def detail(self, call: ToolCall) -> list[str]:
        return [self.header(call)]


class GrepRenderer(ToolRenderer):
    verb = "grep"

    def header(self, call: ToolCall) -> str:
        header = fg("tool_title", "grep ", bold=True) + fg(
            "accent", str(call.params.get("pattern") or "")
        )
        if shown := _elsewhere(call.params.get("path"), call.cwd):
            header += fg("muted", f" in {shown}")
        if glob := call.params.get("glob"):
            header += fg("muted", f" ({glob})")
        if call.finished and call.summary:
            header += fg("dim", f"  {call.summary}")
        return header


class GlobRenderer(ToolRenderer):
    verb = "glob"

    def header(self, call: ToolCall) -> str:
        header = fg("tool_title", "glob ", bold=True) + fg(
            "accent", str(call.params.get("pattern") or "")
        )
        if shown := _elsewhere(call.params.get("path"), call.cwd):
            header += fg("muted", f" in {shown}")
        if call.finished and call.summary:
            header += fg("dim", f"  {call.summary}")
        return header


class TodoRenderer(ToolRenderer):
    verb = "todos"

    MARKERS: ClassVar[dict[str, str]] = {
        "completed": TODO_DONE,
        "in_progress": TODO_ACTIVE,
        "pending": TODO_PENDING,
    }

    def header(self, call: ToolCall) -> str:
        header = fg("tool_title", "todos", bold=True)
        if call.summary:
            header += fg("dim", f"  {call.summary}")
        return header

    def body(self, call: ToolCall) -> list[str]:
        """The list itself, not ``todos=[3 items]``. A plan is worth reading.

        Unless it was rejected, in which case the plan on screen is not the one
        the session holds and the reason is the only useful thing to show.
        """
        if call.is_error and call.output:
            return [fg("error", line) for line in call.output.strip().split("\n")]
        return render_todos(call.params.get("todos"))


def render_todos(todos: Any) -> list[str]:
    """One plan, drawn the same way wherever it appears.

    The inline list and the standalone block used to be two copies of this with
    different colours, so the same plan looked like two different things
    depending on where you were reading it.
    """
    if not isinstance(todos, list) or not todos:
        return []
    out: list[str] = []
    for todo in todos:
        if not isinstance(todo, dict):
            continue
        status = str(todo.get("status") or "pending")
        marker = TodoRenderer.MARKERS.get(status, TODO_PENDING)
        # active_form is optional; falling through to content keeps a todo from
        # rendering as the literal string "None".
        # The plan is the model's own words, so it is sanitized like any
        # other text it wrote - a todo reaches here without passing a block.
        content = plain_text(str(todo.get("content") or ""))
        label = (
            plain_text(str(todo.get("active_form") or content))
            if status == "in_progress"
            else content
        )
        marker_role = {"completed": "success", "in_progress": "accent"}.get(status, "dim")
        label_role = {"completed": "dim", "in_progress": "accent"}.get(status, "muted")
        out.append(fg(marker_role, f"{marker} ") + fg(label_role, label))
    return out


class SymbolsRenderer(ToolRenderer):
    verb = "symbols"

    def header(self, call: ToolCall) -> str:
        mode = str(call.params.get("mode") or "")
        # The subject is the symbol for a search and the file for an outline -
        # whichever one the user would name if they described the call.
        subject = str(call.params.get("symbol") or "")
        header = fg("tool_title", f"symbols {mode} ", bold=True) + fg("accent", subject)
        if shown := _elsewhere(call.params.get("file_path") or call.params.get("path"), call.cwd):
            header += fg("muted", f"{' in ' if subject else ''}{shown}")
        if call.finished and call.summary:
            header += fg("dim", f"  {call.summary}")
        return header


class TaskRenderer(ToolRenderer):
    verb = "task"

    def header(self, call: ToolCall) -> str:
        header = fg("tool_title", "task ", bold=True) + fg(
            "accent", str(call.params.get("subagent_type") or "agent")
        )
        if description := call.params.get("description"):
            header += fg("muted", f"  {description}")
        return header


RENDERERS: dict[str, ToolRenderer] = {
    "read": ReadRenderer(),
    "write": WriteRenderer(),
    "edit": EditRenderer(),
    "bash": BashRenderer(),
    "bashoutput": BashRenderer(),
    "grep": GrepRenderer(),
    "glob": GlobRenderer(),
    "symbols": SymbolsRenderer(),
    "todowrite": TodoRenderer(),
    "task": TaskRenderer(),
}
FALLBACK = ToolRenderer()


def renderer_for(name: str) -> ToolRenderer:
    """The renderer for ``name``, or the generic one for tools HX does not own."""
    return RENDERERS.get(name.lower(), FALLBACK)


def clip(lines: list[str], limit: int, expanded: bool) -> list[str]:
    """Keep ``limit`` lines, appending a note about what was dropped.

    Applied once, to rendered lines, against one number - so how much of a
    thing you see no longer depends on which code path you arrived through.
    """
    ceiling = EXPANDED_MAX if expanded else limit
    if len(lines) <= ceiling:
        return lines
    return [*lines[:ceiling], expand_note(len(lines) - ceiling)]


def _elsewhere(path: Any, cwd: Path) -> str:
    """The path, unless it is the directory the session is already in.

    ``grep TODO in .`` says nothing the user did not already know."""
    shown = display_path(path, cwd)
    return "" if shown in ("", ".") else shown


def _strip_line_number(line: str) -> str:
    """Drop the ``   12\\t`` prefix the read tool adds, keeping the code."""
    head, tab, rest = line.partition("\t")
    return rest if tab and head.strip().isdigit() else line


def _plain_output(call: ToolCall, limit: int) -> list[str]:
    if not call.output.strip():
        return []
    lines, hidden = _head(call.output, limit, call.expanded)
    role = "error" if call.is_error else "tool_output"
    out = [fg(role, line) for line in lines]
    return [*out, expand_note(hidden)] if hidden else out


def _brief(params: dict[str, Any], limit: int = RECORD_WIDTH) -> str:
    """Compact ``key=value`` preview for tools without a renderer of their own."""
    parts: list[str] = []
    for key, value in params.items():
        if isinstance(value, list):
            rendered = f"[{len(value)} items]"
        elif isinstance(value, dict):
            rendered = "{…}"
        elif isinstance(value, str) and cell_width(value) > limit:
            rendered = repr(truncate_to_width(value, limit - 1) + ELLIPSIS)
        else:
            rendered = repr(value)
        parts.append(f"{key}={rendered}")
    joined = ", ".join(parts)
    if cell_width(joined) <= limit:
        return joined
    return truncate_to_width(joined, limit - 1) + ELLIPSIS
