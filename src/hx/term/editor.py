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
mispositioned relative to the text it completes. The list goes *above* the
frame: the dock is pinned to the bottom row, so a list under the text would
push the prompt, the hints and the status bar up the screen by however many
matches the last keystroke happened to leave.
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
    """Map the buffer onto visual rows, wrapping to ``width`` between words.

    The rows of a line are exactly its text, cut up: nothing is added or
    dropped at a break, so a buffer offset maps onto a row and a column by
    counting alone. That is why the space a line breaks on stays at the end
    of its row - see :func:`_wrap_words` - rather than being swallowed.
    """
    if width < 1:
        width = 1

    rows: list[LayoutLine] = []
    offset = 0
    for logical in text.split("\n"):
        chunks = _wrap_words(logical, width)
        for index, chunk in enumerate(chunks):
            length = len(chunk)
            column: int | None = None
            # A cursor at a break belongs to the row it continues onto: what
            # is typed there lands at the start of that row, so the cursor is
            # drawn there too. Past the last row there is nothing to continue.
            last = index == len(chunks) - 1
            if offset <= cursor < offset + length or (cursor == offset + length and last):
                column = cell_width(chunk[: cursor - offset])
            rows.append(LayoutLine(chunk, column))
            offset += length
        offset += 1  # the newline itself
    return rows


def _wrap_words(line: str, width: int) -> list[str]:
    """Break a logical line into rows of at most ``width`` cells, between words.

    A row ends after the last space that fits. The space the break falls on
    may hang one cell past ``width`` - a word that exactly fills a row keeps
    its space rather than pushing it to the front of the next one - which is
    what the editor's right-hand padding is for. A word too long for any row
    breaks where it hits the edge, as does a run that is nothing but spaces.
    """
    if not line:
        return [""]
    clusters = list(grapheme_clusters(line))
    rows: list[str] = []
    start = 0
    used = 0
    # Where the row may end: just past a space with a word before it.
    fold: int | None = None
    worded = False
    index = 0
    while index < len(clusters):
        cluster = clusters[index]
        step = cell_width(cluster)
        if used + step > width and index > start:
            if cluster == " " and worded and used == width:
                end = index + 1  # hang it
            elif fold is not None:
                end = fold
            else:
                end = index
            rows.append("".join(clusters[start:end]))
            start, fold, worded = end, None, False
            used = sum(cell_width(c) for c in clusters[start:index])
            worded = any(c != " " for c in clusters[start:index])
            if end > index:
                index = end
            continue
        if cluster == " ":
            if worded:
                fold = index + 1
        else:
            worded = True
        used += step
        index += 1
    rows.append("".join(clusters[start:]))
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
        #: Extra rows drawn above the editor, inside its own line array, so a
        #: completion list can never be mispositioned relative to the text.
        self.completions: list[str] = []

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

    def set_completions(self, lines: list[str]) -> None:
        """Rows to draw above the frame - the completion list, or nothing."""
        self.completions = lines
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

        out = [fill_line(pad + line, width) for line in self.completions]
        out += [*self._rule(self.top_rule, width, hidden_above, "↑"), *body]
        out += self._rule(self.bottom_rule, width, hidden_below, "↓")
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
        # The cursor may sit one cell past the text column - on a space hanging
        # off a full row, or after the last character of one - and it is drawn
        # there, in the right-hand padding, rather than over the text.
        room = inner + min(1, self._padding_x)

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
            return truncate_to_width(painted, room)

        # At end of line the cursor is an appended cell, which costs a column
        # the text did not need. Without trimming for it the line comes out one
        # cell too wide and the renderer refuses to draw the frame.
        head = truncate_to_width(head, max(0, room - 1))
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
