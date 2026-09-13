"""The multi-line prompt.

Two things make this the hardest component, and both are about the cursor.

It has to be drawn as an **inverse-video cell** rather than left to the
terminal's own cursor, because the real one cannot composite with a background
fill and cannot be positioned inside a line that is about to be redrawn. So the
grapheme under the cursor is inverted, or a space is inverted at end of line -
and the width bookkeeping differs between those two cases, because appending
costs a column and replacing does not.

It also has to tell the terminal where the **hardware** cursor belongs, via a
zero-width marker the screen strips out. An input method's candidate window
follows the hardware cursor, so without that, typing Japanese pops the
candidate list up in the corner of the terminal instead of beside the text.

The editor owns its own frame - a rule above and a rule below, no verticals -
and draws any completion list inside its own line array, so a popup cannot be
mispositioned relative to the text it completes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from hx.term.ansi import fill_line, inverse, terminate
from hx.term.buffer import TextBuffer
from hx.term.component import Widget
from hx.term.primitives import LabelledRule, Rule
from hx.term.screen import CURSOR_MARKER
from hx.term.undo import UndoStack
from hx.term.width import cell_width, grapheme_clusters, truncate_to_width

MIN_VISIBLE_LINES = 5
"""Rows the draft is always allowed, however short the terminal."""

VISIBLE_FRACTION = 0.3
"""Share of the terminal a draft may grow to before it scrolls internally.

A prompt that can eat the screen buries the conversation it is about.
"""


@dataclass(frozen=True, slots=True)
class LayoutLine:
    """One visual row: its text, and where the cursor sits in it, if it does."""

    text: str
    cursor_column: int | None = None


def layout(text: str, width: int, cursor: int) -> list[LayoutLine]:
    """Map the buffer onto visual rows, wrapping to ``width``.

    Wrapping happens at the width, not at a word boundary: a prompt is being
    typed into, and a line that reflows under the cursor as a word grows is
    disorienting in a way it is not in rendered prose.
    """
    if width < 1:
        width = 1

    rows: list[LayoutLine] = []
    offset = 0
    for logical in text.split("\n"):
        chunks = _wrap_hard(logical, width)
        for chunk in chunks:
            length = len(chunk)
            column: int | None = None
            if offset <= cursor <= offset + length:
                # A cursor exactly at a wrap point belongs to the row it
                # continues onto, not the one it just left - except at the very
                # end of the buffer, where there is no next row.
                at_wrap = cursor == offset + length and length == _visible_len(chunk, width)
                if not (at_wrap and chunk is not chunks[-1]):
                    column = cell_width(chunk[: cursor - offset])
            rows.append(LayoutLine(chunk, column))
            offset += length
        offset += 1  # the newline itself
    return rows


def _visible_len(chunk: str, width: int) -> int:
    return len(chunk) if cell_width(chunk) >= width else -1


def _wrap_hard(line: str, width: int) -> list[str]:
    """Break a logical line into rows of at most ``width`` cells."""
    if not line:
        return [""]
    rows: list[str] = []
    current = ""
    used = 0
    for cluster in grapheme_clusters(line):
        step = cell_width(cluster)
        if used + step > width and current:
            rows.append(current)
            current, used = "", 0
        current += cluster
        used += step
    rows.append(current)
    return rows


class Editor(Widget):
    """An editable draft, framed by two rules."""

    def __init__(
        self,
        *,
        padding_x: int = 1,
        rule_color: Callable[[str], str] | None = None,
        rows_available: Callable[[], int] = lambda: 24,
    ) -> None:
        super().__init__()
        self.buffer = TextBuffer()
        self.undo_stack = UndoStack()
        self.placeholder = ""
        self._padding_x = padding_x
        self._rows_available = rows_available
        self._scroll = 0
        # Labelled, because the top rule doubles as the working indicator: a
        # turn starting then costs no layout, and the transcript above does not
        # jump when a spinner appears.
        self.top_rule = LabelledRule(rule_color, align="left")
        self.bottom_rule = Rule(rule_color)
        #: Extra rows drawn under the editor, inside its own line array, so a
        #: completion list can never be mispositioned relative to the text.
        self.footer: list[str] = []

    # -- text --------------------------------------------------------------

    @property
    def text(self) -> str:
        return self.buffer.text

    @text.setter
    def text(self, value: str) -> None:
        self.buffer.set(value)
        self.invalidate()

    def clear(self) -> None:
        self.buffer.clear()
        self.undo_stack.break_run()
        self.invalidate()

    def insert(self, text: str, *, coalesce: bool = False) -> None:
        self.undo_stack.record(self.buffer.text, self.buffer.cursor, coalesce=coalesce)
        self.buffer.insert(text)
        self.invalidate()

    def set_status(self, label: str) -> None:
        """Put a status into the top rule, or clear it with an empty string."""
        self.top_rule.set_label(label)
        self.invalidate()

    def set_footer(self, lines: list[str]) -> None:
        self.footer = lines
        self.invalidate()

    # -- rendering ---------------------------------------------------------

    @property
    def max_visible(self) -> int:
        return max(MIN_VISIBLE_LINES, int(self._rows_available() * VISIBLE_FRACTION))

    def draw(self, width: int) -> list[str]:
        inner = max(1, width - 2 * self._padding_x)
        rows = self._rows(inner)

        visible = self._scrolled(rows)
        pad = " " * self._padding_x

        body: list[str] = []
        for row in visible:
            painted = self._paint(row, inner)
            body.append(fill_line(pad + painted, width))

        hidden_above = self._scroll
        hidden_below = len(rows) - self._scroll - len(visible)

        out = [*self._rule(self.top_rule, width, hidden_above, "↑"), *body]
        out += self._rule(self.bottom_rule, width, hidden_below, "↓")
        out += [fill_line(pad + line, width) for line in self.footer]
        return out

    def _rows(self, inner: int) -> list[LayoutLine]:
        if not self.buffer.text and self.placeholder:
            return [LayoutLine(self.placeholder, 0)]
        return layout(self.buffer.text, inner, self.buffer.cursor)

    def _scrolled(self, rows: list[LayoutLine]) -> list[LayoutLine]:
        """Keep the cursor's row on screen, then clamp."""
        limit = self.max_visible
        if len(rows) <= limit:
            self._scroll = 0
            return rows

        cursor_row = next(
            (index for index, row in enumerate(rows) if row.cursor_column is not None), 0
        )
        if cursor_row < self._scroll:
            self._scroll = cursor_row
        elif cursor_row >= self._scroll + limit:
            self._scroll = cursor_row - limit + 1
        self._scroll = max(0, min(self._scroll, len(rows) - limit))
        return rows[self._scroll : self._scroll + limit]

    def _rule(self, rule: Rule, width: int, hidden: int, arrow: str) -> list[str]:
        """A rule, carrying an overflow count when the draft scrolls."""
        from hx.term.primitives import LabelledRule

        if hidden <= 0:
            return rule.render(width)
        labelled = LabelledRule(rule._color, f"{arrow} {hidden} more", "center")
        return labelled.render(width)

    def _paint(self, row: LayoutLine, inner: int) -> str:
        """One visual row, with the cursor drawn into it."""
        placeholder = not self.buffer.text and self.placeholder
        if row.cursor_column is None:
            return truncate_to_width(row.text, inner)

        clusters = list(grapheme_clusters(row.text))
        consumed = 0
        before: list[str] = []
        under = ""
        after: list[str] = []
        for cluster in clusters:
            if consumed < row.cursor_column:
                before.append(cluster)
            elif not under:
                under = cluster
            else:
                after.append(cluster)
            consumed += cell_width(cluster)

        head = "".join(before)
        if placeholder:
            # Nothing has been typed; the cursor sits before the hint rather
            # than replacing its first character.
            return CURSOR_MARKER + inverse(" ") + truncate_to_width(row.text, max(0, inner - 1))

        if under:
            painted = head + CURSOR_MARKER + inverse(under) + "".join(after)
            return truncate_to_width(painted, inner)

        # At end of line the cursor is an appended cell, which costs a column
        # the text did not need. Without trimming for it the line comes out one
        # cell too wide and the renderer refuses to draw the frame.
        head = truncate_to_width(head, max(0, inner - 1))
        return head + CURSOR_MARKER + inverse(" ")

    # -- editing helpers ---------------------------------------------------

    def kill_range(self, start: int, end: int) -> str:
        self.undo_stack.record(self.buffer.text, self.buffer.cursor)
        killed = self.buffer.delete_range(start, end)
        self.invalidate()
        return killed

    def undo(self) -> bool:
        snapshot = self.undo_stack.undo(self.buffer.text, self.buffer.cursor)
        if snapshot is None:
            return False
        self.buffer.set(snapshot.text, snapshot.cursor)
        self.invalidate()
        return True

    def redo(self) -> bool:
        snapshot = self.undo_stack.redo(self.buffer.text, self.buffer.cursor)
        if snapshot is None:
            return False
        self.buffer.set(snapshot.text, snapshot.cursor)
        self.invalidate()
        return True


__all__ = ["Editor", "LayoutLine", "layout", "terminate"]
