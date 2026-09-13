"""Getting the document onto the terminal, touching as little as possible.

The document is a flat list of lines. Drawing it means reconciling that list
against the one already on screen, and the shape of the reconciliation is what
makes the transcript real scrollback rather than a repainted buffer:

* Lines that have scrolled off the top are **never** rewritten. They belong to
  the terminal now - it scrolls them, selects them and copies them, and they
  survive this process exiting.
* New content is appended with ``\\r\\n``, which is what pushes the old lines
  up into that scrollback in the first place.
* Only the changed tail is redrawn, so a spinner ticking beside a long session
  rewrites one line rather than the screen.

A change *above* the visible region cannot be expressed by moving the cursor,
because the cursor cannot reach it. That forces a full repaint, and the only
common cause is a resize - which invalidates the wrapping of every line anyway.

Every update is wrapped in synchronized-output mode, so the terminal is never
caught presenting half a frame.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from hx.term.ansi import SEGMENT_RESET, terminate
from hx.term.component import Component
from hx.term.terminal import Terminal
from hx.term.width import cell_width, strip_ansi

SYNC_START = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"
ERASE_LINE = "\x1b[2K"
ERASE_BELOW = "\x1b[J"
CLEAR_ALL = "\x1b[2J\x1b[H\x1b[3J"
CURSOR_HIDE = "\x1b[?25l"
CURSOR_SHOW = "\x1b[?25h"

CURSOR_MARKER = "\x1b_hx:c\x07"
"""Zero-width marker naming where the hardware cursor belongs.

The visible cursor is drawn by the editor as an inverse-video cell, because
that composites correctly with a background fill and the real cursor does not.
But an input method's candidate window follows the *hardware* cursor, so it
has to be parked in the right place too - otherwise typing Japanese pops the
candidate list up in the corner of the terminal, nowhere near the text.
"""

_MARKER_PATTERN = re.compile(re.escape(CURSOR_MARKER))

WRITE_CHUNK = 1 << 20
"""Bytes per write. A long transcript's first paint is megabytes, and handing
that to the OS as one string is how a terminal ends up with a stalled pipe."""


class LineTooWide(RuntimeError):
    """A component returned a line wider than the width it was given.

    Fatal on purpose. One over-wide line wraps, which shifts every line below
    it by one, which makes every subsequent diff wrong - so the failure is
    loud, names the component's output, and happens at the seam where it can
    still be attributed.
    """


class MainScreen:
    """Draws a component tree into the terminal's normal screen."""

    def __init__(
        self,
        terminal: Terminal,
        root: Component,
        *,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._terminal = terminal
        self._root = root
        self._on_error = on_error

        self._previous: list[str] = []
        self._previous_width = -1
        self._previous_height = -1
        #: Where the hardware cursor sits, as an index into ``_previous``.
        self._cursor_row = 0
        self._cursor_visible = False

    # -- drawing -----------------------------------------------------------

    def render(self) -> None:
        """Reconcile the document with what is on screen."""
        width, height = self._terminal.size
        lines = [terminate(line) for line in self._root.render(width)]
        lines, cursor = self._extract_cursor(lines, width)
        self._check_widths(lines, width)

        if not self._previous:
            self._paint_all(lines, clear=False)
        elif width != self._previous_width or height != self._previous_height:
            # Wrapping changed, so every line already on screen is wrong.
            self._paint_all(lines, clear=True)
        else:
            first = _first_difference(self._previous, lines)
            if first is None:
                self._place_cursor(cursor, lines)
                return
            viewport_top = max(0, len(self._previous) - height)
            if first < viewport_top:
                # Above the fold: unreachable by cursor movement.
                self._paint_all(lines, clear=True)
            else:
                self._paint_from(first, lines)

        self._previous = lines
        self._previous_width = width
        self._previous_height = height
        self._place_cursor(cursor, lines)

    def _paint_all(self, lines: list[str], *, clear: bool) -> None:
        out = [SYNC_START, CURSOR_HIDE]
        if clear:
            out.append(CLEAR_ALL)
        out.append("\r")
        for index, line in enumerate(lines):
            if index:
                out.append("\r\n")
            out.append(ERASE_LINE)
            out.append(line)
        out.append(ERASE_BELOW)
        out.append(SYNC_END)
        self._cursor_row = max(0, len(lines) - 1)
        self._write("".join(out))

    def _paint_from(self, first: int, lines: list[str]) -> None:
        """Rewrite from the first changed line to the end of the document.

        Rewriting the tail rather than only the changed lines keeps the cursor
        arithmetic trivial, and the tail is short in the case that matters -
        the dock at the bottom, which is where almost every repaint starts.
        """
        out = [SYNC_START, CURSOR_HIDE, self._move_to(first)]
        for index in range(first, len(lines)):
            if index > first:
                out.append("\r\n")
            out.append(ERASE_LINE)
            out.append(lines[index])

        if len(lines) < len(self._previous):
            # The document shrank; the lines it used to occupy are still there.
            out.append(ERASE_BELOW)

        out.append(SYNC_END)
        self._cursor_row = max(first, len(lines) - 1)
        self._write("".join(out))

    def _move_to(self, row: int) -> str:
        """Cursor movement from where it is to the start of ``row``."""
        delta = row - self._cursor_row
        if delta < 0:
            return f"\x1b[{-delta}A\r"
        if delta > 0:
            return "\r\n" * delta + "\r"
        return "\r"

    # -- the cursor --------------------------------------------------------

    def _extract_cursor(
        self, lines: list[str], width: int
    ) -> tuple[list[str], tuple[int, int] | None]:
        """Pull the cursor marker out, remembering where it was."""
        position: tuple[int, int] | None = None
        cleaned: list[str] = []
        for row, line in enumerate(lines):
            if CURSOR_MARKER not in line:
                cleaned.append(line)
                continue
            before = line.split(CURSOR_MARKER, 1)[0]
            column = min(cell_width(before), max(0, width - 1))
            position = (row, column)
            cleaned.append(_MARKER_PATTERN.sub("", line))
        return cleaned, position

    def _place_cursor(self, cursor: tuple[int, int] | None, lines: list[str]) -> None:
        if cursor is None:
            if self._cursor_visible:
                self._write(CURSOR_HIDE)
                self._cursor_visible = False
            return

        row, column = cursor
        movement = self._move_to(row)
        self._cursor_row = row
        forward = f"\x1b[{column}C" if column else ""
        self._write(movement + forward + CURSOR_SHOW)
        self._cursor_visible = True

    def park_below(self) -> None:
        """Leave the cursor on a fresh line under the document.

        So the shell prompt appears after the transcript rather than on top of
        its last line.
        """
        self._write(self._move_to(max(0, len(self._previous) - 1)) + "\r\n" + CURSOR_SHOW)
        self._cursor_visible = True

    # -- invariants --------------------------------------------------------

    def _check_widths(self, lines: list[str], width: int) -> None:
        for index, line in enumerate(lines):
            measured = cell_width(line)
            if measured <= width:
                continue
            report = self._width_report(lines, index, measured, width)
            if self._on_error is not None:
                self._on_error(report)
            raise LineTooWide(report)

    def _width_report(self, lines: list[str], index: int, measured: int, width: int) -> str:
        window = lines[max(0, index - 2) : index + 3]
        rows = [
            f"  {number:>4}  {cell_width(line):>4}  {strip_ansi(line)!r}"
            for number, line in enumerate(window, start=max(0, index - 2))
        ]
        return "\n".join(
            [
                f"line {index} is {measured} cells wide at width {width}",
                f"  {'row':>4}  {'cells':>4}  text",
                *rows,
            ]
        )

    # -- output ------------------------------------------------------------

    def _write(self, data: str) -> None:
        """Write in bounded chunks.

        A resumed session's first paint can be megabytes, and handing that over
        as a single string is how a terminal ends up with a stalled pipe.
        """
        for start in range(0, len(data), WRITE_CHUNK):
            self._terminal.write(data[start : start + WRITE_CHUNK])


def _first_difference(before: list[str], after: list[str]) -> int | None:
    """Index of the first line that changed, or ``None`` if none did."""
    for index in range(min(len(before), len(after))):
        if before[index] != after[index]:
            return index
    if len(before) != len(after):
        return min(len(before), len(after))
    return None


__all__ = ["CURSOR_MARKER", "SEGMENT_RESET", "LineTooWide", "MainScreen"]
