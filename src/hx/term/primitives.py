"""The whole vocabulary the UI is built from.

Deliberately small. There is one way to draw a line of text, one way to tint a
block, one way to separate two things vertically, and one way to draw a rule -
and there is no box, anywhere. A rectangle around every section turns a dialog
into a stack of rectangles; a rule above and below turns it into a section.

The spacing vocabulary is 0 and 1. ``Text(..., padding_x=1)`` for a line of
chat or dialog, ``Box(1, 1, tint)`` for a block that belongs to somebody, and
``Spacer(1)`` between things - emitted by whatever produced the block, so that
two adjacent blocks cannot both contribute a blank line.
"""

from __future__ import annotations

from collections.abc import Callable

from hx.term.ansi import SEGMENT_RESET, fill_line, terminate, wrap
from hx.term.component import Component, Widget
from hx.term.width import cell_width, truncate_to_width

Tint = Callable[[str], str]
"""Wraps a line in a background. :meth:`hx.tui.theme.Theme.tint` makes them."""

CURSOR = "→ "
"""Where Enter will land. Two cells, so unselected rows align under it."""

CURRENT = "✓ "
"""The value already in force. Sits beside :data:`CURSOR`, never instead of it."""

GUTTER = "  "
"""An unselected row, occupying exactly what a marker would."""


class Text(Widget):
    """One run of text, wrapped to the width and padded out to it.

    ``padding_x`` is the one-column breathing room every line of chat and every
    line of a dialog gets, so that nothing is flush against the terminal edge.
    ``padding_y`` adds blank rows inside the same background, which is what
    makes a tinted block look like a block rather than a highlighted line.
    """

    __slots__ = ("_padding_x", "_padding_y", "_text", "_tint")

    def __init__(
        self,
        text: str = "",
        padding_x: int = 1,
        padding_y: int = 0,
        tint: Tint | None = None,
    ) -> None:
        super().__init__()
        self._text = text
        self._padding_x = padding_x
        self._padding_y = padding_y
        self._tint = tint

    @property
    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.invalidate()

    def draw(self, width: int) -> list[str]:
        inner = max(1, width - 2 * self._padding_x)
        pad = " " * self._padding_x

        body = [f"{pad}{line}" for line in wrap(self._text, inner)]
        blank = [""] * self._padding_y
        return [self._line(line, width) for line in [*blank, *body, *blank]]

    def _line(self, line: str, width: int) -> str:
        if self._tint is None:
            return fill_line(line, width)
        return terminate(self._tint(_pad_to(line, width)))


class HangingText(Widget):
    """A prefixed block whose continuation lines indent under the text.

    A marker applied to the first line only is the difference between::

        · the model switched to claude-sonnet-4.5
        because the previous one was unavailable

    and::

        · the model switched to claude-sonnet-4.5
          because the previous one was unavailable

    The indent applies to wrapped lines and to explicit newlines alike, which
    matters because most multi-line output is built by joining lines and has no
    idea how wide the terminal is.
    """

    __slots__ = ("_body", "_padding_x", "_prefix", "_prefix_width", "_tint")

    def __init__(
        self,
        prefix: str,
        body: str,
        padding_x: int = 1,
        tint: Tint | None = None,
    ) -> None:
        super().__init__()
        self._prefix = prefix
        self._prefix_width = cell_width(prefix)
        self._body = body
        self._padding_x = padding_x
        self._tint = tint

    def set_body(self, body: str) -> None:
        if body != self._body:
            self._body = body
            self.invalidate()

    def draw(self, width: int) -> list[str]:
        pad = " " * self._padding_x
        inner = max(1, width - 2 * self._padding_x - self._prefix_width)
        hang = " " * self._prefix_width

        out: list[str] = []
        for paragraph in self._body.split("\n"):
            for index, line in enumerate(wrap(paragraph, inner)):
                marker = self._prefix if not out and index == 0 else hang
                out.append(f"{pad}{marker}{line}")

        if self._tint is None:
            return [fill_line(line, width) for line in out]
        return [terminate(self._tint(_pad_to(line, width))) for line in out]


class Box(Widget):
    """A tinted block of children.

    The tint is the only thing marking the block, and it reaches the full width
    of the terminal so the block reads as a band rather than a ragged column.
    Backgrounds mean one of three things in this UI - whose turn this is, how a
    tool call ended, or that a run of text is code - and are never used for
    emphasis.
    """

    __slots__ = ("_padding_x", "_padding_y", "_tint", "children")

    def __init__(
        self,
        padding_x: int = 1,
        padding_y: int = 1,
        tint: Tint | None = None,
        *children: Component,
    ) -> None:
        super().__init__()
        self._padding_x = padding_x
        self._padding_y = padding_y
        self._tint = tint
        self.children: list[Component] = list(children)

    def add(self, child: Component) -> Component:
        self.children.append(child)
        self.invalidate()
        return child

    def clear(self) -> None:
        self.children.clear()
        self.invalidate()

    def render(self, width: int) -> list[str]:
        inner = max(1, width - 2 * self._padding_x)
        pad = " " * self._padding_x

        body: list[str] = []
        for child in self.children:
            for line in child.render(inner):
                body.append(f"{pad}{_strip_terminator(line)}")

        blank = [""] * self._padding_y
        lines = [*blank, *body, *blank]
        if self._tint is None:
            return [fill_line(line, width) for line in lines]
        return [terminate(self._tint(_pad_to(line, width))) for line in lines]

    def draw(self, width: int) -> list[str]:  # pragma: no cover - render overrides
        return self.render(width)


class Spacer(Widget):
    """Blank rows. The only vertical separator in the UI.

    Emitted by whatever produced the block below it, never by the container, so
    that two blocks in a row cannot each contribute one and leave a double gap.
    """

    __slots__ = ("_lines",)

    def __init__(self, lines: int = 1) -> None:
        super().__init__()
        self._lines = lines

    def draw(self, width: int) -> list[str]:
        return [""] * self._lines


class Rule(Widget):
    """A horizontal line, full width. The only border in the UI.

    One character repeated - no corners, no verticals, nothing to misalign at a
    narrow width or to break when a glyph is measured wrong.
    """

    __slots__ = ("_char", "_color")

    def __init__(self, color: Callable[[str], str] | None = None, char: str = "─") -> None:
        super().__init__()
        self._color = color
        self._char = char

    def set_color(self, color: Callable[[str], str] | None) -> None:
        self._color = color
        self.invalidate()

    def draw(self, width: int) -> list[str]:
        line = self._char * max(1, width)
        return [terminate(self._color(line) if self._color else line)]


class LabelledRule(Rule):
    """A rule carrying a label, so a status line costs no row of its own.

    This is where the working spinner lives: the prompt is framed by two rules
    whether or not a turn is running, so starting one changes no layout and the
    transcript above it does not jump.

    The label is dropped rather than clipped when it will not fit, because a
    half-drawn status is worse than none - the rule still reads as a frame.
    """

    __slots__ = ("_align", "_label")

    def __init__(
        self,
        color: Callable[[str], str] | None = None,
        label: str = "",
        align: str = "left",
        char: str = "─",
    ) -> None:
        super().__init__(color, char)
        self._label = label
        self._align = align

    def set_label(self, label: str) -> None:
        if label != self._label:
            self._label = label
            self.invalidate()

    def draw(self, width: int) -> list[str]:
        width = max(1, width)
        if not self._label:
            return super().draw(width)

        label = f" {self._label} "
        label_width = cell_width(label)
        # Two cells of rule on the shorter side, so the label reads as set into
        # the line rather than as having replaced it.
        if label_width + 4 > width:
            return super().draw(width)

        left = (width - label_width) // 2 if self._align == "center" else 2
        right = width - label_width - left

        paint = self._color or (lambda text: text)
        return [terminate(paint(self._char * left) + label + paint(self._char * right))]


class Lines(Widget):
    """Already-rendered lines, passed through with a left pad.

    The seam between the parts of the UI that build strings - a diff painter, a
    syntax highlighter - and the parts that build components.
    """

    __slots__ = ("_lines", "_padding_x", "_tint")

    def __init__(self, lines: list[str], padding_x: int = 1, tint: Tint | None = None) -> None:
        super().__init__()
        self._lines = lines
        self._padding_x = padding_x
        self._tint = tint

    def set_lines(self, lines: list[str]) -> None:
        self._lines = lines
        self.invalidate()

    def draw(self, width: int) -> list[str]:
        pad = " " * self._padding_x
        inner = max(1, width - 2 * self._padding_x)
        out = [f"{pad}{truncate_to_width(_strip_terminator(line), inner)}" for line in self._lines]
        if self._tint is None:
            return [fill_line(line, width) for line in out]
        return [terminate(self._tint(_pad_to(line, width))) for line in out]


def _strip_terminator(line: str) -> str:
    """Drop a line's trailing reset so it can be embedded inside another."""
    return line[: -len(SEGMENT_RESET)] if line.endswith(SEGMENT_RESET) else line


def _pad_to(line: str, width: int) -> str:
    used = cell_width(line)
    if used > width:
        line = truncate_to_width(line, width)
        used = cell_width(line)
    return line + " " * (width - used)
