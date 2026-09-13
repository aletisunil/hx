"""The differ, checked against what a terminal would actually display.

Asserting on escape sequences tests the implementation; asserting on a VT
emulator's screen tests the behaviour. So the bytes go through pyte, and the
assertions are about what the user would see - plus, separately, the two
properties that cannot be seen but are the whole point: that finished lines
are appended into scrollback and never rewritten.
"""

from __future__ import annotations

import pyte
import pytest

from hx.term.component import Container
from hx.term.primitives import Rule, Spacer, Text
from hx.term.screen import CLEAR_ALL, CURSOR_MARKER, LineTooWide, MainScreen
from hx.term.terminal import FakeTerminal
from hx.term.width import cell_width


class Harness:
    """A screen, a fake terminal, and a VT emulator fed from it.

    Two views of the output, and the distinction matters: :attr:`emitted` is
    what was written since the last :meth:`mark`, for asserting how much work a
    frame did; :meth:`display` replays everything ever written, for asserting
    what the user would be looking at.
    """

    def __init__(self, columns: int = 40, rows: int = 12) -> None:
        self.terminal = FakeTerminal(columns, rows)
        self.root = Container()
        self.screen = MainScreen(self.terminal, self.root)
        self._before = ""
        self._vt = pyte.Screen(columns, rows)
        self._stream = pyte.Stream(self._vt)

    def render(self) -> None:
        self.screen.render()

    def mark(self) -> None:
        """Start a fresh window for :attr:`emitted`, keeping the transcript."""
        self._before += self.terminal.output
        self.terminal.clear_output()

    def display(self) -> list[str]:
        self._vt.reset()
        self._stream.feed(self._before + self.terminal.output)
        return [line.rstrip() for line in self._vt.display]

    def visible(self) -> list[str]:
        return [line for line in self.display() if line]

    @property
    def emitted(self) -> str:
        return self.terminal.output


def test_a_first_paint_shows_the_document() -> None:
    h = Harness()
    h.root.add(Text("hello"))
    h.root.add(Text("world"))
    h.render()
    assert h.visible() == [" hello", " world"]


def test_a_first_paint_does_not_clear_the_screen() -> None:
    """Whatever the user had in their terminal before running hx is theirs."""
    h = Harness()
    h.root.add(Text("hello"))
    h.render()
    assert CLEAR_ALL not in h.emitted


def test_an_unchanged_document_writes_nothing() -> None:
    h = Harness()
    h.root.add(Text("steady"))
    h.render()
    h.mark()
    h.render()
    assert h.emitted == ""


def test_changing_the_last_line_rewrites_only_that_line() -> None:
    """A spinner beside a long transcript must cost one line, not a screen."""
    h = Harness()
    for index in range(6):
        h.root.add(Text(f"line {index}"))
    spinner = h.root.add(Text("tick"))
    h.render()
    h.mark()

    spinner.set_text("tock")
    h.render()

    assert h.emitted.count("\r\n") == 0, "moved to another row to write one line"
    assert CLEAR_ALL not in h.emitted
    assert "tock" in h.emitted
    assert "line 0" not in h.emitted


def test_appending_leaves_the_earlier_lines_untouched() -> None:
    """Appended with \\r\\n, so the old lines scroll into the terminal's own
    scrollback - where the terminal, not hx, owns them."""
    h = Harness()
    h.root.add(Text("first"))
    h.render()
    h.mark()

    h.root.add(Text("second"))
    h.render()

    assert "\r\n" in h.emitted, "new content is appended, not repainted"
    assert CLEAR_ALL not in h.emitted
    assert "first" not in h.emitted, "an already-drawn line was rewritten"
    assert h.visible() == [" first", " second"]


def test_a_resize_forces_a_full_repaint() -> None:
    """Every line's wrapping changed, so everything on screen is wrong."""
    h = Harness()
    h.root.add(Text("the quick brown fox jumps over the lazy dog"))
    h.render()
    h.mark()

    h.terminal.resize(20, 12)
    h.render()
    assert CLEAR_ALL in h.emitted


def test_a_height_change_also_repaints() -> None:
    h = Harness()
    h.root.add(Text("hello"))
    h.render()
    h.mark()
    h.terminal.resize(40, 6)
    h.render()
    assert CLEAR_ALL in h.emitted


def test_a_change_above_the_visible_region_forces_a_repaint() -> None:
    """The cursor cannot reach a line that has scrolled off, so a diff there
    cannot be expressed as a movement."""
    h = Harness(columns=40, rows=5)
    top = h.root.add(Text("top"))
    for index in range(10):
        h.root.add(Text(f"filler {index}"))
    h.render()
    h.mark()

    top.set_text("changed")
    h.render()
    assert CLEAR_ALL in h.emitted


def test_a_shrinking_document_clears_what_it_used_to_occupy() -> None:
    """Otherwise the tail of the longer version stays on screen forever."""
    h = Harness()
    for index in range(5):
        h.root.add(Text(f"row {index}"))
    h.render()

    h.root.clear()
    h.root.add(Text("only"))
    h.render()
    assert h.visible() == [" only"]


def test_a_line_becoming_shorter_does_not_leave_its_old_tail() -> None:
    h = Harness()
    line = h.root.add(Text("a very long line of text"))
    h.render()
    line.set_text("short")
    h.render()
    assert h.visible() == [" short"]


def test_every_update_is_wrapped_in_synchronized_output() -> None:
    """So the terminal is never caught presenting half a frame."""
    h = Harness()
    h.root.add(Text("hello"))
    h.render()
    assert h.emitted.startswith("\x1b[?2026h")
    assert "\x1b[?2026l" in h.emitted


def test_the_cursor_marker_is_removed_and_never_drawn() -> None:
    h = Harness()
    h.root.add(Text(f"ab{CURSOR_MARKER}cd"))
    h.render()
    assert h.visible() == [" abcd"]
    assert CURSOR_MARKER not in h.emitted


def test_the_cursor_marker_parks_the_hardware_cursor() -> None:
    """An input method's candidate window follows the real cursor, so typing
    Japanese must not pop the candidate list up in the corner."""
    h = Harness()
    h.root.add(Text("first"))
    h.root.add(Text(f"ab{CURSOR_MARKER}cd"))
    h.render()
    assert "\x1b[?25h" in h.emitted, "the cursor is shown where it was parked"
    assert "\x1b[3C" in h.emitted, "one pad column plus 'ab'"


def test_without_a_marker_the_cursor_stays_hidden() -> None:
    h = Harness()
    h.root.add(Text("no cursor here"))
    h.render()
    assert "\x1b[?25h" not in h.emitted


def test_an_over_wide_line_is_a_loud_failure() -> None:
    """It would wrap, shifting every line below it by one and making every
    later diff wrong. Better to fail at the seam where it can be attributed."""

    class Overflowing(Text):
        def draw(self, width: int) -> list[str]:
            return ["x" * (width + 5)]

    h = Harness()
    h.root.add(Overflowing())
    with pytest.raises(LineTooWide, match="cells wide at width"):
        h.render()


def test_the_failure_report_names_the_offending_line() -> None:
    class Overflowing(Text):
        def draw(self, width: int) -> list[str]:
            return ["fine", "way too wide " * 10]

    reports: list[str] = []
    h = Harness()
    h.screen = MainScreen(h.terminal, h.root, on_error=reports.append)
    h.root.add(Overflowing())
    with pytest.raises(LineTooWide):
        h.render()
    assert reports and "way too wide" in reports[0]
    assert "row" in reports[0] and "cells" in reports[0]


def test_wide_characters_land_where_the_terminal_puts_them() -> None:
    h = Harness()
    h.root.add(Text("日本語 text"))
    h.render()
    assert h.visible() == [" 日本語 text"]


def test_the_document_survives_a_long_sequence_of_edits() -> None:
    """The differ's state has to stay in step with the screen across many
    partial updates, which is exactly where an off-by-one hides."""
    h = Harness(columns=40, rows=24)
    rows = [h.root.add(Text(f"row {index}")) for index in range(8)]
    h.render()

    for step in range(20):
        rows[step % len(rows)].set_text(f"row {step % len(rows)} v{step}")
        h.render()

    expected = [line.text for line in rows]
    assert h.visible() == [f" {text}" for text in expected]


def test_parking_below_leaves_the_shell_prompt_its_own_line() -> None:
    h = Harness()
    h.root.add(Text("last line"))
    h.render()
    h.mark()
    h.screen.park_below()
    assert h.emitted.endswith("\x1b[?25h")
    assert "\r\n" in h.emitted


@pytest.mark.parametrize("width", [10, 20, 40, 100])
def test_nothing_ever_exceeds_the_width(width: int) -> None:
    h = Harness(columns=width, rows=20)
    h.root.add(Text("the quick brown fox jumps over the lazy dog"))
    h.root.add(Rule())
    h.root.add(Spacer(1))
    h.root.add(Text("日本語のテキスト 👨‍👩‍👧‍👦"))
    h.render()
    for line in h.display():
        assert cell_width(line) <= width


def test_emoji_reach_the_terminal_whole_even_though_pyte_cannot_show_them() -> None:
    """pyte drops the continuation of a ZWJ sequence, so the emulator is the
    wrong instrument for this one thing. Recorded here so that the gap between
    what we emit and what the harness displays is not mistaken for a bug.
    """
    text = "family 👨‍👩‍👧‍👦 flag 🇯🇵"
    h = Harness(columns=40, rows=6)
    h.root.add(Text(text))
    h.render()

    emitted = h.emitted
    assert text in emitted, "the renderer dropped part of a grapheme cluster"
    assert "‍👩" not in "".join(h.display()), "pyte grew ZWJ support; use it here"
