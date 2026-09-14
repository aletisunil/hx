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
from hx.term.screen import CLEAR_ALL, CURSOR_MARKER, ERASE_BELOW, LineTooWide, MainScreen
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


def test_closing_leaves_the_cursor_where_the_document_started() -> None:
    """The shell prompt comes back on the row HX took, not below the frame."""
    h = docked_harness(rows=12, footer=1)
    h.root.add(Text("last line"))
    h.root.add(Text("dock"))
    h.render()
    h.mark()
    h.screen.close()
    assert h.emitted.endswith("\x1b[?25h")
    assert ERASE_BELOW in h.emitted


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


# --- the fullscreen rectangle ------------------------------------------------
#
# The second mode, which `/fullscreen` turns on. The properties asserted here
# are the ones that make it feel like a window rather than a transcript: the
# dock is on the last row whatever the conversation did, the rows above it
# scroll under the app's control, and neither of those ever touches the
# terminal's scrollback.


class Footer(Container):
    """A document whose trailing ``rows`` lines are pinned, like the dock."""

    def __init__(self, rows: int = 1) -> None:
        super().__init__()
        self._footer_rows = rows

    def footer_height(self, width: int) -> int:
        return self._footer_rows


def fullscreen_harness(columns: int = 40, rows: int = 8, footer: int = 1) -> Harness:
    h = Harness(columns, rows)
    h.root = Footer(footer)
    h.screen = MainScreen(h.terminal, h.root)
    return h


def test_fullscreen_enters_the_alternate_screen() -> None:
    h = fullscreen_harness()
    h.root.add(Text("hello"))
    h.screen.set_fullscreen(True)
    h.render()
    assert h.terminal.alt_screen, "the terminal was never switched"


def test_fullscreen_pins_the_dock_to_the_last_row() -> None:
    """The prompt is where the user last saw it, not under the conversation."""
    h = fullscreen_harness(rows=8, footer=1)
    h.root.add(Text("one"))
    h.root.add(Text("two"))
    h.root.add(Text("prompt"))
    h.screen.set_fullscreen(True)
    h.render()

    display = h.display()
    assert display[0] == " one"
    assert display[1] == " two"
    assert display[-1] == " prompt", "the dock left the bottom row"
    assert display[2:-1] == [""] * 5, "the gap belongs between the two, not after"


def test_fullscreen_shows_the_newest_output_when_the_document_is_too_tall() -> None:
    h = fullscreen_harness(rows=6, footer=1)
    for index in range(20):
        h.root.add(Text(f"line {index}"))
    h.root.add(Text("prompt"))
    h.screen.set_fullscreen(True)
    h.render()

    display = h.display()
    assert display[-1] == " prompt"
    assert display[-2] == " line 19", "the viewport is not pinned to the newest line"


def test_scrolling_moves_the_viewport_and_keeps_the_dock() -> None:
    h = fullscreen_harness(rows=6, footer=1)
    for index in range(20):
        h.root.add(Text(f"line {index}"))
    h.root.add(Text("prompt"))
    h.screen.set_fullscreen(True)
    h.render()

    assert h.screen.scroll_by(-3) is True
    h.render()
    display = h.display()
    assert display[-1] == " prompt", "scrolling took the dock with it"
    assert display[-2] == " line 16"

    assert h.screen.scroll_to_bottom() is True
    h.render()
    assert h.display()[-2] == " line 19"


def test_scrolling_stops_at_the_top_and_the_bottom() -> None:
    h = fullscreen_harness(rows=6, footer=1)
    for index in range(8):
        h.root.add(Text(f"line {index}"))
    h.root.add(Text("prompt"))
    h.screen.set_fullscreen(True)
    h.render()

    assert h.screen.scroll_to_top() is True
    h.render()
    assert h.display()[0] == " line 0"
    assert h.screen.scroll_by(-5) is False, "scrolled past the first line"

    assert h.screen.scroll_to_bottom() is True
    assert h.screen.scroll_by(3) is False, "scrolled past the newest line"


def test_scrolling_does_nothing_on_the_normal_screen() -> None:
    """The terminal is the scroller there, and a key that fought it would be a
    worse version of the scrollbar the user already has."""
    h = Harness()
    h.root.add(Text("hello"))
    h.render()
    assert h.screen.scroll_by(-5) is False
    assert h.screen.scroll_to_top() is False
    assert h.screen.scroll_to_bottom() is False


def test_fullscreen_rewrites_only_the_rows_that_changed() -> None:
    h = fullscreen_harness(rows=8, footer=1)
    for index in range(4):
        h.root.add(Text(f"line {index}"))
    spinner = h.root.add(Text("tick"))
    h.screen.set_fullscreen(True)
    h.render()
    h.mark()

    spinner.set_text("tock")
    h.render()
    assert "tock" in h.emitted
    assert "line 0" not in h.emitted, "a full repaint for one changed row"


def test_leaving_fullscreen_puts_the_terminal_back() -> None:
    h = fullscreen_harness()
    h.root.add(Text("hello"))
    h.screen.set_fullscreen(True)
    h.render()
    h.screen.set_fullscreen(False)
    assert h.terminal.alt_screen is False


# --- clearing ----------------------------------------------------------------


def test_clear_erases_the_screen_and_the_scrollback() -> None:
    """3J is the scrollback half, and it is what makes /clear clear rather
    than merely scroll."""
    h = Harness()
    h.root.add(Text("old conversation"))
    h.render()
    h.mark()

    h.root.clear()
    h.root.add(Text("fresh"))
    h.screen.clear()

    assert "\x1b[3J" in h.emitted
    assert h.visible() == [" fresh"]


def test_a_resize_in_fullscreen_keeps_the_document_and_the_dock() -> None:
    """Every line's wrapping changed and the rectangle is a different size, so
    this is the one case where the whole screen is rewritten."""
    h = fullscreen_harness(columns=40, rows=12, footer=3)
    for index in range(5):
        h.root.add(Text(f"line {index}"))
    for index in range(3):
        h.root.add(Text(f"dock {index}"))
    h.screen.set_fullscreen(True)
    h.render()
    assert h.display()[-3:] == [" dock 0", " dock 1", " dock 2"]

    h.terminal.resize(30, 10)
    h.render()

    # The harness replays into an emulator of the original size, so what is
    # asserted here is the content, not which row it landed on.
    visible = h.visible()
    assert visible[0] == " line 0", "the transcript went missing on resize"
    assert visible[-3:] == [" dock 0", " dock 1", " dock 2"], "the dock left the bottom"


# --- the dock on the normal screen -------------------------------------------
#
# The scrollback renderer appends, so a document shorter than the window would
# leave the dock two rows under the banner with the rest of the screen empty
# below it. The gap goes above the dock instead, and drains as the
# conversation grows - without ever rewriting a line the terminal has taken.


def docked_harness(columns: int = 40, rows: int = 12, footer: int = 1) -> Harness:
    h = Harness(columns, rows)
    h.root = Footer(footer)
    h.screen = MainScreen(h.terminal, h.root)
    return h


def test_the_dock_starts_on_the_bottom_row() -> None:
    h = docked_harness(rows=8, footer=1)
    h.root.add(Text("banner"))
    h.root.add(Text("prompt"))
    h.render()

    display = h.display()
    assert display[0] == " banner"
    assert display[-1] == " prompt", "the dock is not on the bottom row"
    assert display[1:-1] == [""] * 6, "the gap belongs above the dock"


def test_the_gap_drains_as_the_conversation_grows() -> None:
    """The dock stays put; the transcript fills the screen towards it."""
    h = docked_harness(rows=8, footer=1)
    h.root.add(Text("banner"))
    prompt = Text("prompt")
    h.root.add(prompt)

    for index in range(3):
        h.root.children.insert(len(h.root.children) - 1, Text(f"line {index}"))
        h.render()
        assert h.display()[-1] == " prompt", "the dock left the bottom row"

    assert h.visible() == [" banner", " line 0", " line 1", " line 2", " prompt"]


def test_growing_past_the_gap_never_clears_the_scrollback() -> None:
    """The padding is gone by the time the terminal starts scrolling, so the
    lines it scrolls away are the terminal's and are never repainted."""
    h = docked_harness(rows=8, footer=1)
    h.root.add(Text("banner"))
    prompt = Text("prompt")
    h.root.add(prompt)
    h.render()

    for index in range(20):
        h.root.children.insert(len(h.root.children) - 1, Text(f"line {index}"))
        h.mark()
        h.render()
        assert CLEAR_ALL not in h.emitted, "a repaint threw away the scrollback"

    assert h.display()[-1] == " prompt"


def test_a_conversation_taller_than_the_screen_is_not_padded() -> None:
    h = docked_harness(rows=6, footer=1)
    for index in range(10):
        h.root.add(Text(f"line {index}"))
    h.root.add(Text("prompt"))
    h.render()

    assert h.display() == [
        " line 5",
        " line 6",
        " line 7",
        " line 8",
        " line 9",
        " prompt",
    ]


def test_a_spinner_beside_a_gap_still_rewrites_one_line() -> None:
    """The padding must not turn every dock tick into a repaint of the tail."""
    h = docked_harness(rows=12, footer=1)
    h.root.add(Text("banner"))
    prompt = h.root.add(Text("tick"))
    h.render()
    h.mark()

    prompt.set_text("tock")
    h.render()

    assert h.emitted.count("\r\n") == 0, "moved to another row to write one line"
    assert "banner" not in h.emitted, "an already-drawn line was rewritten"


def test_a_document_without_a_dock_is_left_where_it_is() -> None:
    """Nothing to pin, and padding it would push blank rows under the last
    line and make every later one-line redraw a repaint of the tail."""
    h = Harness(rows=8)
    h.root.add(Text("hello"))
    h.render()
    assert h.display()[0] == " hello"
    assert h.emitted.count("\r\n") == 0, "blank rows were appended under the document"


def test_a_shrinking_document_keeps_the_dock_on_the_bottom_row() -> None:
    """A picker closing gives rows back, but the terminal does not scroll
    backwards - so the space it frees has to open above the dock."""
    h = docked_harness(rows=20, footer=1)
    body = [h.root.add(Text(f"line {index}")) for index in range(30)]
    h.root.add(Text("prompt"))
    h.render()
    assert h.display()[-1] == " prompt"

    for block in body[25:]:  # still taller than the screen afterwards
        h.root.children.remove(block)
    h.render()

    display = h.display()
    assert display[-1] == " prompt", "the dock floated up the screen"
    assert display[13] == " line 24", "the transcript moved instead of the gap"
    assert display[14:-1] == [""] * 5, "the freed rows opened below the dock"


def test_a_shrinking_document_does_not_clear_the_scrollback() -> None:
    h = docked_harness(rows=20, footer=1)
    body = [h.root.add(Text(f"line {index}")) for index in range(30)]
    h.root.add(Text("prompt"))
    h.render()
    h.mark()

    for block in body[25:]:
        h.root.children.remove(block)
    h.render()

    assert CLEAR_ALL not in h.emitted


def test_closing_takes_the_frame_off_the_screen() -> None:
    """Exiting leaves the terminal as HX found it, not a dead prompt box."""
    h = docked_harness(rows=12, footer=1)
    h.root.add(Text("what was said"))
    h.root.add(Text("dock"))
    h.render()
    assert h.display()[-1] == " dock", "the dock never reached the bottom row"

    h.screen.close()

    assert h.visible() == [], "something HX drew is still on the window"


def test_closing_leaves_the_scrollback_above_the_window_alone() -> None:
    """``ESC[3J`` is the only way to reach it and it takes the whole of it -
    including what was in the terminal before HX ran."""
    h = docked_harness(rows=12, footer=1)
    for index in range(30):
        h.root.add(Text(f"line {index}"))
    h.root.add(Text("dock"))
    h.render()
    h.mark()

    h.screen.close()

    assert "\x1b[3J" not in h.emitted
    assert h.visible() == []


def test_closing_a_session_that_started_fullscreen_has_nothing_to_shed() -> None:
    """It never drew on the normal screen, so leaving the alternate one is the
    whole of it."""
    h = fullscreen_harness()
    h.root.add(Text("hello"))
    h.screen.set_fullscreen(True)
    h.render()
    h.mark()

    h.screen.close()

    assert h.emitted == "\x1b[?1049l"


def test_closing_in_fullscreen_erases_the_frame_parked_under_it() -> None:
    """``/fullscreen`` covers the normal screen rather than clearing it, so the
    frame drawn before the switch is still there when the session ends. Leaving
    it behind puts the shell prompt under a dead prompt box."""
    h = fullscreen_harness(rows=8, footer=1)
    h.root.add(Text("what was said"))
    h.root.add(Text("dock"))
    h.render()
    h.screen.set_fullscreen(True)
    h.render()
    h.mark()

    h.screen.close()

    assert h.emitted.startswith("\x1b[?1049l"), "the alternate screen was not left first"
    assert ERASE_BELOW in h.emitted, "the frame under it was never erased"
