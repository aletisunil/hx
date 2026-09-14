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
* A document shorter than the window is padded above its dock, so the prompt
  opens on the bottom row instead of creeping down the screen a block at a
  time. The padding drains as the conversation grows, and grows again when the
  document shrinks - the terminal does not scroll backwards, so rows given
  back by a closing picker have to open above the dock. Either way the dock is
  on the bottom row and no line that has become scrollback is ever moved.

A change *above* the visible region cannot be expressed by moving the cursor,
because the cursor cannot reach it. That forces a full repaint, and the only
common cause is a resize - which invalidates the wrapping of every line anyway.

``/fullscreen`` switches this to :class:`MainScreen`'s second mode. There the
document is not appended to the terminal at all: a window of it is painted into
a fixed rectangle on the alternate screen, addressed by absolute row, with the
dock pinned to the last line and the rows above scrolled by the app rather than
by the terminal. It is the same document and the same components - only where
the lines land changes - so nothing above this module knows which mode it is
in.

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

        self._alt = False
        self._painted: list[str] = []
        """The rectangle currently on the alternate screen, row by row."""
        self._scroll = 0
        """Rows the fullscreen viewport is lifted off the bottom of the
        document. Zero - the default - pins it to the newest line, which is
        where a conversation wants to be."""
        self._parked: tuple[list[str], int] = ([], 0)
        """The frame left on the normal screen when ``/fullscreen`` covered it.

        The alternate screen hides it rather than erasing it, so it is still
        there when the session ends - and :meth:`close` has to erase it, or the
        shell prompt comes back under a dead prompt box from before the switch.
        """

    # -- modes -------------------------------------------------------------

    @property
    def fullscreen(self) -> bool:
        return self._alt

    def set_fullscreen(self, enabled: bool) -> None:
        """Move between the scrollback renderer and the fullscreen one.

        The buffer being drawn into is about to be a different one, so nothing
        already on screen can be diffed against: the next render repaints in
        full, in whichever mode this leaves behind.

        On the way up, what was on the normal screen is remembered rather than
        forgotten: the alternate screen only covers it, and whoever ends the
        session has to be able to take it off again.
        """
        if enabled == self._alt:
            return
        # Coming back down the caller clears the screen, so there is nothing
        # parked any more; going up, this frame is what will be waiting.
        self._parked = (list(self._previous), self._cursor_row) if enabled else ([], 0)
        self._alt = enabled
        self._scroll = 0
        setter = getattr(self._terminal, "set_alt_screen", None)
        if setter is not None:
            setter(enabled)
        self.invalidate()

    def invalidate(self) -> None:
        """Forget what is on screen, so the next render paints all of it.

        For everything that changes the screen behind the renderer's back: a
        theme swap, a resumed session after ctrl+z, a shell command that wrote
        over the transcript.
        """
        self._previous = []
        self._previous_width = -1
        self._previous_height = -1
        self._cursor_row = 0

    def clear(self) -> None:
        """Erase the screen, and the scrollback above it, then repaint.

        ``ESC[3J`` is the scrollback half, and it is what makes ``/clear``
        clear rather than merely scroll. Terminals that do not implement it
        ignore it - Apple's Terminal.app is the one people hit - and there is
        the screen half for them.
        """
        self._write(CLEAR_ALL)
        self.invalidate()
        self.render()

    # -- scrolling ---------------------------------------------------------
    #
    # Only in fullscreen. On the normal screen the terminal is the scroller,
    # and a key that fought it would be a worse version of the scrollbar the
    # user already has.

    def scroll_by(self, rows: int) -> bool:
        """Move the fullscreen viewport, returning whether anything moved."""
        if not self._alt:
            return False
        height = self._terminal.size[1]
        highest = max(0, len(self._previous) - height)
        target = max(0, min(highest, self._scroll - rows))
        if target == self._scroll:
            return False
        self._scroll = target
        return True

    def scroll_to_top(self) -> bool:
        if not self._alt:
            return False
        height = self._terminal.size[1]
        return self.scroll_by(-(max(0, len(self._previous) - height) - self._scroll))

    def scroll_to_bottom(self) -> bool:
        if not self._alt or self._scroll == 0:
            return False
        self._scroll = 0
        return True

    # -- drawing -----------------------------------------------------------

    def render(self) -> None:
        """Reconcile the document with what is on screen."""
        width, height = self._terminal.size
        lines = [terminate(line) for line in self._root.render(width)]
        if not self._alt:
            lines = self._seat_footer(lines, width, height)
        lines, cursor = self._extract_cursor(lines, width)
        self._check_widths(lines, width)

        if self._alt:
            self._render_viewport(lines, cursor, width, height)
            return

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

    # -- the fullscreen rectangle ------------------------------------------

    def _render_viewport(
        self, lines: list[str], cursor: tuple[int, int] | None, width: int, height: int
    ) -> None:
        """Paint a window of the document into a fixed rectangle.

        The dock is pinned to the bottom of the screen and the transcript
        scrolls above it, which is the whole point of the mode: the prompt is
        where it was a minute ago rather than wherever the conversation
        happened to end.
        """
        footer = self._footer_height(width)
        body = lines[: max(0, len(lines) - footer)]
        tail = lines[len(body) :]
        room = max(0, height - len(tail))

        highest = max(0, len(body) - room)
        self._scroll = min(self._scroll, highest)
        end = len(body) - self._scroll
        start = max(0, end - room)
        window = body[start:end]
        # Short conversation: the gap goes between it and the dock, so the
        # transcript starts at the top and the prompt stays on the last row.
        window = window + [""] * (room - len(window))
        frame = (window + tail)[:height]

        repaint = width != self._previous_width or height != self._previous_height
        out = [SYNC_START, CURSOR_HIDE]
        if repaint:
            out.append(CLEAR_ALL)
        for row, line in enumerate(frame):
            if not repaint and row < len(self._painted) and self._painted[row] == line:
                continue
            out.append(f"\x1b[{row + 1};1H{ERASE_LINE}{line}")
        out.append(SYNC_END)
        self._write("".join(out))

        self._previous = lines
        self._previous_width = width
        self._previous_height = height
        self._painted = frame
        self._place_cursor_in_frame(cursor, len(body), start, room, height)

    # -- seating the dock --------------------------------------------------

    def _seat_footer(self, lines: list[str], width: int, height: int) -> list[str]:
        """Blank rows between the conversation and the dock, so the dock sits
        on the bottom row of a screen the conversation has not filled yet.

        Without them a new session draws its prompt two rows under the banner
        with two thirds of the window empty below it, and the dock then creeps
        down the screen a block at a time until the conversation is finally
        tall enough to hold it at the bottom. Padding is the difference, so it
        exists only while the document is shorter than the screen and reaches
        zero at the moment the terminal starts scrolling: nothing has left the
        screen yet, so nothing that belongs to scrollback is being moved.

        A document with no footer has nothing to pin - padding it would only
        push blank rows under the last line and turn every later one-line
        redraw into a repaint of the tail - so it is left alone.
        """
        footer = self._footer_height(width)
        if footer <= 0 or footer >= height:
            return lines
        target = height
        if self._previous and width == self._previous_width and height == self._previous_height:
            # Rows that have scrolled off the top cannot be handed back: the
            # terminal does not scroll backwards. So a document that shrinks -
            # a picker closing, a block collapsing - keeps the rows it has, and
            # the space it gave up opens above the dock rather than below it.
            target = max(target, len(self._previous))
        missing = target - len(lines)
        if missing <= 0:
            return lines
        split = max(0, len(lines) - footer)
        return lines[:split] + [""] * missing + lines[split:]

    def _footer_height(self, width: int) -> int:
        """Rows at the end of the document that stay on the bottom row.

        Asked of the document rather than assumed, because what is pinned is a
        property of the UI - the dock - and this module draws lines.
        """
        measure = getattr(self._root, "footer_height", None)
        if measure is None:
            return 0
        try:
            return max(0, int(measure(width)))
        except Exception:  # pragma: no cover - a document that will not measure
            return 0

    def _place_cursor_in_frame(
        self,
        cursor: tuple[int, int] | None,
        body_rows: int,
        start: int,
        room: int,
        height: int,
    ) -> None:
        """Put the hardware cursor where its document row landed on screen."""
        if cursor is None:
            if self._cursor_visible:
                self._write(CURSOR_HIDE)
                self._cursor_visible = False
            return
        document_row, column = cursor
        if document_row >= body_rows:  # in the dock, which is pinned
            row = room + (document_row - body_rows)
        elif start <= document_row < start + room:
            row = document_row - start
        else:  # scrolled out of the window
            if self._cursor_visible:
                self._write(CURSOR_HIDE)
                self._cursor_visible = False
            return
        row = max(0, min(height - 1, row))
        self._cursor_row = row
        self._write(f"\x1b[{row + 1};{column + 1}H{CURSOR_SHOW}")
        self._cursor_visible = True

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

    def close(self) -> None:
        """Take the frame off the screen, leaving the terminal as it was found.

        Everything HX drew that is still on the window is erased and the cursor
        is left on the row the document started on, so the shell prompt comes
        back where it was rather than under a dead prompt box, a row of keys
        that no longer do anything and a status bar for a session that has
        ended.

        The erase stops at the top of the window on purpose. Lines that scrolled
        above it are in the terminal's scrollback, and the only sequence that
        reaches them - ``ESC[3J`` - takes the whole of it, including whatever
        was in the terminal before HX ran. Throwing that away to tidy up after
        ourselves is not ours to do; ``/clear`` is the command that asks for it.

        A session that ends in fullscreen leaves the alternate screen here
        rather than in :meth:`ProcessTerminal.restore`, because what is
        uncovered underneath is this renderer's problem: a session that ran on
        the normal screen before ``/fullscreen`` left a frame there, and it is
        still on the window. Leaving first puts the cursor back where entering
        saved it - the row that frame was parked at - and the erase below then
        takes it off exactly as if fullscreen had never happened. A session
        that started fullscreen parked nothing and stops after the switch.
        """
        if self._alt:
            self._alt = False
            setter = getattr(self._terminal, "set_alt_screen", None)
            if setter is not None:
                setter(False)
            self._previous, self._cursor_row = self._parked
            self._parked = ([], 0)
        if not self._previous:
            return
        height = self._terminal.size[1]
        top = max(0, len(self._previous) - height)
        self._write(self._move_to(top) + ERASE_BELOW + CURSOR_SHOW)
        self._cursor_row = top
        self._cursor_visible = True
        self._previous = []

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
