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

from pathlib import Path
from typing import Any

from hx.term.component import Widget
from hx.term.markdown import render_markdown
from hx.term.primitives import Box, HangingText, Lines, Text
from hx.tui.glyphs import NOTICE, SPINNER, TOOL_DONE, TOOL_FAILED
from hx.tui.limits import PREVIEW_LINES
from hx.tui.paint import ThemePainter, fg, tint
from hx.tui.renderers import ToolCall, renderer_for

_PAINTER = ThemePainter()


class UserMessage(Widget):
    """What the user said, tinted the width of the terminal.

    Markdown is rendered rather than shown raw, because a user pasting a code
    block or a list meant it as one.
    """

    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    @property
    def text(self) -> str:
        return self._text

    def draw(self, width: int) -> list[str]:
        body = Lines(
            [fg("user_text", line) for line in render_markdown(self._text, max(1, width - 2))],
            padding_x=0,
        )
        return Box(1, 1, tint("user_bg"), body).render(width)


class AssistantMessage(Widget):
    """What the model said. No background at all.

    The contrast with the user's tinted block is what marks the turn; adding a
    second treatment here would just make the transcript busier.
    """

    __slots__ = ("_text",)

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self._text = text

    def append(self, chunk: str) -> None:
        self._text += chunk
        self.invalidate()

    def set_text(self, text: str) -> None:
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
    """The model's reasoning, in its own muted colour."""

    __slots__ = ("_collapsed", "_text")

    def __init__(self, text: str = "", collapsed: bool = True) -> None:
        super().__init__()
        self._text = text
        self._collapsed = collapsed

    def append(self, chunk: str) -> None:
        self._text += chunk
        self.invalidate()

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed != self._collapsed:
            self._collapsed = collapsed
            self.invalidate()

    def draw(self, width: int) -> list[str]:
        if not self._text.strip():
            return []
        if self._collapsed:
            return Text(fg("thinking", "Thinking…", italic=True)).render(width)
        lines = [fg("thinking", line, italic=True) for line in self._text.strip().split("\n")]
        return Lines(lines).render(width)


class ToolBlock(Widget):
    """One tool call: a header, optionally some output, tinted by its state.

    The tint is the state machine. Running, succeeded and failed are three
    backgrounds rather than three labels, so a long transcript can be skimmed
    for the red one without reading any of it.
    """

    __slots__ = ("_call", "_frame")

    def __init__(self, call: ToolCall) -> None:
        super().__init__()
        self._call = call
        self._frame = 0

    @property
    def call(self) -> ToolCall:
        return self._call

    def update(self, **changes: Any) -> None:
        from dataclasses import replace

        self._call = replace(self._call, **changes)
        self.invalidate()

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
        self._text = text
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
