"""The things that appear in a transcript.

Four kinds, and the difference between them is carried by weight rather than
by labels: the user's turn is a tinted block, the model's reply is bare text,
a tool call is a block tinted by how it ended, and a notice is one line with a
bullet.

That asymmetry between the user's block and the model's is the speaker
distinction. It does not need a ``You:`` prefix, and a prefix would cost a
column on every line for information the colour already carries.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hx.term.component import Widget
from hx.term.markdown import render_markdown
from hx.term.primitives import Box, HangingText, Lines
from hx.term.sanitize import plain_text
from hx.tui.glyphs import IMAGE, NOTICE, SPINNER, TOOL_DONE, TOOL_FAILED
from hx.tui.limits import PREVIEW_LINES, STREAM_TAIL
from hx.tui.paint import ThemePainter, fg, tint
from hx.tui.renderers import ToolCall, renderer_for, sanitized_call, sanitized_fields

if TYPE_CHECKING:
    from hx.core.messages import ImageBlock

_PAINTER = ThemePainter()


class UserMessage(Widget):
    """What the user said, tinted the width of the terminal.

    Markdown is rendered rather than shown raw, because a user pasting a code
    block or a list meant it as one. Sanitized for the same reason: a paste is
    whatever was on the clipboard, escape sequences and all.
    """

    __slots__ = ("_images", "_text")

    def __init__(self, text: str, images: Sequence[ImageBlock] = ()) -> None:
        super().__init__()
        self._text = plain_text(text)
        self._images = tuple(images)

    @property
    def text(self) -> str:
        return self._text

    def draw(self, width: int) -> list[str]:
        inner = max(1, width - 2)
        rendered = render_markdown(self._text, inner, breaks=True)
        lines = [fg("user_text", line) for line in rendered]
        # Named under the text rather than drawn: a terminal cannot show the
        # picture, but it can show which one went and how big it was.
        lines += [fg("muted", _clip(IMAGE + _described(image), inner)) for image in self._images]
        return Box(1, 1, tint("user_bg"), Lines(lines, padding_x=0)).render(width)


class AssistantMessage(Widget):
    """What the model said. No background at all.

    The contrast with the user's tinted block is what marks the turn; adding a
    second treatment here would just make the transcript busier.
    """

    __slots__ = ("_text",)

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self._text = plain_text(text)

    def append(self, chunk: str) -> None:
        # Per chunk rather than over the whole buffer, so a long reply is
        # sanitized once end to end instead of once per delta.
        self._text += plain_text(chunk)
        self.invalidate()

    def set_text(self, text: str) -> None:
        text = plain_text(text)
        if text != self._text:
            self._text = text
            self.invalidate()

    @property
    def text(self) -> str:
        return self._text

    def draw(self, width: int) -> list[str]:
        if not self._text.strip():
            return []
        return Lines(render_markdown(self._text, max(1, width - 2), _PAINTER)).render(width)


class ThinkingMessage(Widget):
    """The model's reasoning, in its own muted colour.

    Shown rather than hidden. Reasoning is the part of a turn that explains the
    part you can see, and a reader who does not want it has a key for that -
    ``ctrl+o``, the same one that expands a tool call. A one-line ``Thinking…``
    with no way to open it, which is what this was, is the reasoning simply
    not arriving.
    """

    __slots__ = ("_collapsed", "_text")

    def __init__(self, text: str = "", collapsed: bool = False) -> None:
        super().__init__()
        self._text = plain_text(text)
        self._collapsed = collapsed

    def append(self, chunk: str) -> None:
        self._text += plain_text(chunk)
        self.invalidate()

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed != self._collapsed:
            self._collapsed = collapsed
            self.invalidate()

    def toggle(self) -> None:
        self.set_collapsed(not self._collapsed)

    @property
    def text(self) -> str:
        return self._text

    def draw(self, width: int) -> list[str]:
        if not self._text.strip():
            return []
        lines = [fg("thinking", line, italic=True) for line in self._text.strip().split("\n")]
        if not self._collapsed:
            return Lines(lines).render(width)
        # Folded by the reader, with the key they just pressed - so the count
        # is what is worth saying here, not the key again.
        note = fg("dim", f" ({len(lines)} lines)") if len(lines) > 1 else ""
        return Lines([fg("thinking", "Thinking…", italic=True) + note]).render(width)


def _stream_tail(text: str) -> str:
    """The last :data:`STREAM_TAIL` characters, from a line start so the cut
    cannot land inside an escape sequence."""
    if len(text) <= STREAM_TAIL:
        return text
    tail = text[-STREAM_TAIL:]
    return tail[tail.find("\n") + 1 :]


class ToolBlock(Widget):
    """One tool call: a header, optionally some output, tinted by its state.

    The tint is the state machine. Running, succeeded and failed are three
    backgrounds rather than three labels, so a long transcript can be skimmed
    for the red one without reading any of it.
    """

    __slots__ = ("_call", "_frame", "_stream", "_stream_dirty", "_streamed")

    def __init__(self, call: ToolCall) -> None:
        super().__init__()
        self._call = sanitized_call(call)
        self._frame = 0
        self._stream: list[str] = []
        """What the tool has streamed, raw; the output is its sanitized form."""
        self._streamed = 0
        self._stream_dirty = False

    @property
    def call(self) -> ToolCall:
        self._fold_stream()
        return self._call

    def update(self, **changes: Any) -> None:
        """Replace fields on the call. Whole values only.

        Only what is being replaced is sanitized. The fields already on the
        call went through this on the way in, and re-scanning them would mean a
        ``update(status=...)`` walking every byte of a long command's stdout
        for the sake of a field that is not a string.

        Streaming output goes through :meth:`append_output` instead, for the
        same reason one step further: passing the growing buffer here would
        rescan it per chunk, which is quadratic in the length of the output.
        """
        from dataclasses import replace

        if "output" in changes:
            self._stream, self._streamed, self._stream_dirty = [], 0, False
        else:
            self._fold_stream()
        self._call = replace(self._call, **sanitized_fields(changes))
        self.invalidate()

    def append_output(self, chunk: str) -> None:
        """Add one streamed chunk.

        Held raw and folded into the output once per frame rather than once
        per chunk, which kept copying the whole buffer and was quadratic in the
        length of the output. Sanitized as a whole when folded, so an escape
        split across two chunks goes as a whole instead of leaving ``[31m``
        behind. Only the last :data:`STREAM_TAIL` characters are kept: the
        block shows a tail, and a command that prints for an hour must not
        keep every byte of it in the transcript.
        """
        if not self._stream:
            self._stream, self._streamed = [self._call.output], len(self._call.output)
        self._stream.append(chunk)
        self._streamed += len(chunk)
        if self._streamed > 2 * STREAM_TAIL:
            tail = _stream_tail("".join(self._stream))
            self._stream, self._streamed = [tail], len(tail)
        self._stream_dirty = True
        self.invalidate()

    def _fold_stream(self) -> None:
        if not self._stream_dirty:
            return
        from dataclasses import replace

        raw = _stream_tail("".join(self._stream))
        self._stream, self._streamed, self._stream_dirty = [raw], len(raw), False
        self._call = replace(self._call, output=plain_text(raw))

    def tick(self) -> None:
        """Advance the spinner. Only meaningful while the call is running."""
        if not self._call.finished:
            self._frame = (self._frame + 1) % len(SPINNER)
            self.invalidate()

    def toggle(self) -> None:
        self.update(expanded=not self._call.expanded)

    @property
    def _state(self) -> str:
        if not self._call.finished:
            return "running"
        return "error" if self._call.is_error else "done"

    def draw(self, width: int) -> list[str]:
        self._fold_stream()
        state = self._state
        marker, marker_role, background = {
            "running": (SPINNER[self._frame], "accent", "tool_pending_bg"),
            "done": (TOOL_DONE, "success", "tool_success_bg"),
            "error": (TOOL_FAILED, "error", "tool_error_bg"),
        }[state]

        renderer = renderer_for(self._call.name)
        lines = [fg(marker_role, f"{marker} ") + renderer.header(self._call)]
        # The body is indented to sit under the header's text rather than under
        # its marker, so the block has one left edge instead of two.
        lines += [f"  {line}" for line in renderer.body(self._call)]

        return Box(1, 1, tint(background), Lines(lines, padding_x=0)).render(width)


class Notice(Widget):
    """One line of housekeeping: a mode change, an error, a command's output.

    The bullet hangs, so a multi-line notice indents under its text. Half of
    the slash commands used to compensate for the lack of that by hand and half
    did not, which is why command output used to arrive at two different
    indents depending on which command you ran.
    """

    __slots__ = ("_level", "_text")

    def __init__(self, text: str, level: str = "info") -> None:
        super().__init__()
        # An error message is very often a tool's stderr wearing a sentence.
        self._text = plain_text(text)
        self._level = level

    @property
    def level(self) -> str:
        return self._level

    def draw(self, width: int) -> list[str]:
        role = {"error": "error", "warning": "warning", "success": "success"}.get(
            self._level, "muted"
        )
        bullet = NOTICE.get(self._level, NOTICE["info"])
        return HangingText(fg(role, bullet), fg(role, self._text)).render(width)


class TodoBlock(Widget):
    """The current plan.

    A block in the transcript rather than a sidebar: in a scrollback-native UI
    there is no column to put a sidebar in, and a plan re-emitted when it
    changes reads as part of the conversation anyway.
    """

    __slots__ = ("_todos",)

    def __init__(self, todos: Any) -> None:
        super().__init__()
        self._todos = todos

    def draw(self, width: int) -> list[str]:
        from hx.tui.renderers import render_todos

        rows = render_todos(self._todos)
        if not rows:
            return []
        done = sum(1 for t in self._todos if isinstance(t, dict) and t.get("status") == "completed")
        header = fg("accent", "Todos", bold=True) + fg("dim", f"  {done}/{len(self._todos)}")
        return Lines([header, *rows]).render(width)


def tool_call(
    name: str,
    params: dict[str, Any],
    cwd: Path,
    **extra: Any,
) -> ToolCall:
    """Build a :class:`~hx.tui.renderers.ToolCall`, defaults included."""
    return ToolCall(name=name, params=params, cwd=cwd, **extra)


__all__ = [
    "PREVIEW_LINES",
    "AssistantMessage",
    "Notice",
    "ThinkingMessage",
    "TodoBlock",
    "ToolBlock",
    "UserMessage",
    "tool_call",
]


def _described(image: ImageBlock) -> str:
    from hx.core.images import describe

    return plain_text(describe(image))


def _clip(text: str, width: int) -> str:
    from hx.term.width import truncate_to_width

    return truncate_to_width(text, width)
