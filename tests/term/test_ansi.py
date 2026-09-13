"""Colour emission, and the reset discipline that keeps blocks from tearing."""

from __future__ import annotations

import pytest

from hx.term.ansi import (
    BG_RESET,
    FG_RESET,
    SEGMENT_RESET,
    active_background,
    bg,
    detect_color_mode,
    fg,
    fill_line,
    hyperlink,
    inverse,
    rgb_to_256,
    terminate,
    wrap,
)
from hx.term.width import cell_width, strip_ansi


def test_foreground_resets_only_the_foreground() -> None:
    """A full reset here would punch a hole in any block behind the text."""
    out = fg("#ff0000", "hi")
    assert out.endswith(FG_RESET)
    assert "\x1b[0m" not in out


def test_background_resets_only_the_background() -> None:
    out = bg("#202020", "hi")
    assert out.endswith(BG_RESET)
    assert "\x1b[0m" not in out


def test_a_coloured_span_survives_inside_a_filled_block() -> None:
    """This composition is the whole reason for the split resets."""
    block = bg("#202020", "before " + fg("#ff0000", "red") + " after")
    assert block.count(BG_RESET) == 1, "the background closes once, at the end"
    assert BG_RESET not in block[: block.index("after")], "and not in the middle"
    assert strip_ansi(block) == "before red after"


def test_truecolor_and_256_emit_different_escapes() -> None:
    assert fg("#5f87ff", "x", "truecolor").startswith("\x1b[38;2;95;135;255m")
    assert fg("#5f87ff", "x", "256color").startswith("\x1b[38;5;")


@pytest.mark.parametrize(
    ("hex_color", "expected"),
    [
        ("#000000", 16),
        ("#ffffff", 231),
        ("#ff0000", 196),
        ("#00ff00", 46),
        ("#0000ff", 21),
    ],
)
def test_the_quantiser_finds_the_exact_cube_entry(hex_color: str, expected: int) -> None:
    from hx.term.ansi import hex_to_rgb

    assert rgb_to_256(*hex_to_rgb(hex_color)) == expected


def test_a_tinted_grey_does_not_snap_to_true_grey() -> None:
    """Most of a good terminal palette is near-neutral but tinted. Losing the
    tint is how a quantised theme turns into a monochrome one."""
    # #8abeb7 is the dark theme's accent: a desaturated teal.
    assert rgb_to_256(0x8A, 0xBE, 0xB7) < 232, "picked a grey for a teal"


def test_a_true_neutral_does_use_the_grey_ramp() -> None:
    assert 232 <= rgb_to_256(0x50, 0x50, 0x50) <= 255


def test_the_ansi_theme_defers_to_the_terminal_palette() -> None:
    assert fg("ansi_red", "x") == "\x1b[31mx" + FG_RESET
    assert fg("ansi_bright_cyan", "x") == "\x1b[96mx" + FG_RESET
    assert fg("ansi_default", "x") == "x", "the terminal's own colour, already in force"


def test_an_empty_colour_means_leave_it_alone() -> None:
    assert fg("", "x") == "x"
    assert bg("", "x") == "x"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"COLORTERM": "truecolor"}, "truecolor"),
        ({"COLORTERM": "24bit"}, "truecolor"),
        ({"TERM_PROGRAM": "ghostty"}, "truecolor"),
        ({"TERM": "xterm-kitty"}, "truecolor"),
        ({"TERM": "xterm-256color"}, "256color"),
        ({}, "256color"),
        ({"COLORTERM": "truecolor", "HX_TRUE_COLOR": "0"}, "256color"),
        ({"TERM": "dumb", "HX_TRUE_COLOR": "1"}, "truecolor"),
    ],
)
def test_colour_mode_detection(env: dict[str, str], expected: str) -> None:
    assert detect_color_mode(env) == expected


def test_every_terminated_line_closes_styles_and_hyperlinks() -> None:
    assert terminate("x").endswith(SEGMENT_RESET)
    assert terminate(terminate("x")) == terminate("x"), "idempotent"


def test_a_hyperlink_is_invisible_to_width() -> None:
    link = hyperlink("file:///tmp/a.py", "a.py")
    assert cell_width(link) == 4


def test_inversion_is_available_but_narrow() -> None:
    """It means the cursor, or a changed word in a diff. Nothing else."""
    assert inverse("x") == "\x1b[7mx\x1b[27m"


def test_a_filled_line_reaches_the_right_edge() -> None:
    """A block only as wide as its longest line reads as a ragged column."""
    line = fill_line("hi", 20, "\x1b[48;2;32;32;32m")
    assert cell_width(line) == 20
    assert line.endswith(SEGMENT_RESET)


def test_a_filled_line_never_exceeds_the_width() -> None:
    assert cell_width(fill_line("far too long for this", 8)) == 8


def test_the_active_background_is_what_gets_reopened_after_a_break() -> None:
    assert active_background("\x1b[48;2;1;2;3mx") == "\x1b[48;2;1;2;3m"
    assert active_background("\x1b[48;5;17mx") == "\x1b[48;5;17m"
    assert active_background("\x1b[41mx") == "\x1b[41m"
    assert active_background("\x1b[48;2;1;2;3mx\x1b[49m") == ""
    assert active_background("\x1b[48;2;1;2;3mx\x1b[0m") == ""
    assert active_background("plain") == ""


def test_wrapping_breaks_on_words_and_fits() -> None:
    lines = wrap("the quick brown fox jumps over the lazy dog", 12)
    assert all(cell_width(line) <= 12 for line in lines)
    assert " ".join(strip_ansi(line).strip() for line in lines).split() == (
        ["the", "quick", "brown", "fox", "jumps", "over", "the", "lazy", "dog"]
    )


def test_wrapping_breaks_an_unbreakable_word_rather_than_overflowing() -> None:
    lines = wrap("/a/very/long/path/without/any/spaces/at/all.py", 10)
    assert all(cell_width(line) <= 10 for line in lines)
    assert "".join(strip_ansi(line) for line in lines) == (
        "/a/very/long/path/without/any/spaces/at/all.py"
    )


def test_wrapping_reopens_the_background_on_the_next_line() -> None:
    """Otherwise a filled block is torn down its right-hand side."""
    tinted = "\x1b[48;2;32;32;32m" + "alpha beta gamma delta epsilon"
    lines = wrap(tinted, 12)
    assert len(lines) > 1
    assert all("\x1b[48;2;32;32;32m" in line for line in lines), lines


def test_wrapping_respects_explicit_newlines() -> None:
    assert [strip_ansi(x) for x in wrap("a\nb", 40)] == ["a", "b"]


def test_wrapping_never_splits_an_escape_sequence() -> None:
    styled = fg("#ff0000", "alpha beta gamma delta")
    for line in wrap(styled, 9):
        assert "\x1b" not in strip_ansi(line)
        assert cell_width(line) <= 9
