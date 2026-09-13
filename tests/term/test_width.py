"""Cell widths, which every other invariant in the renderer is checked against."""

from __future__ import annotations

import pytest

from hx.term.width import (
    cell_width,
    grapheme_clusters,
    pad_to_width,
    strip_ansi,
    truncate_to_width,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("hello", 5),
        ("  indented", 10),
        ("~/project (main)", 16),
    ],
)
def test_ascii_is_its_own_length(text: str, expected: int) -> None:
    assert cell_width(text) == expected


def test_styling_occupies_no_cells() -> None:
    assert cell_width("\x1b[38;2;212;212;212mhello\x1b[39m") == 5
    assert cell_width("\x1b[1m\x1b[4mbold underlined\x1b[0m") == 15


def test_a_hyperlink_occupies_only_its_label() -> None:
    """OSC 8 wraps the text in a URL the terminal does not draw."""
    link = "\x1b]8;;file:///tmp/x.py\x07x.py\x1b]8;;\x07"
    assert cell_width(link) == 4
    assert strip_ansi(link) == "x.py"


def test_our_own_cursor_marker_occupies_no_cells() -> None:
    """The APC marker parks the hardware cursor; it is never drawn."""
    assert cell_width("ab\x1b_hx:c\x07c") == 3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("日本語", 6),  # East Asian Wide: two cells each
        ("ｆｕｌｌ", 8),  # Fullwidth forms
        ("한국어", 6),
        ("café", 4),  # precomposed
        ("café", 4),  # e + combining acute: one glyph, one cell
    ],
)
def test_wide_and_combining_characters(text: str, expected: int) -> None:
    assert cell_width(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("🎉", 2),
        ("👍🏽", 2),  # skin tone modifier rides along
        ("👨‍👩‍👧‍👦", 2),  # ZWJ family: one glyph
        ("🇯🇵", 2),  # a flag is two regional indicators
        ("🇯🇵🇰🇷", 4),  # but two flags are two glyphs
        ("⚠️", 2),  # VS16 asks for the wide emoji presentation
        ("⚠︎", 1),  # VS15 asks for the narrow text presentation
        ("1️⃣", 2),  # keycap
    ],
)
def test_emoji_are_measured_as_glyphs_not_code_points(text: str, expected: int) -> None:
    assert cell_width(text) == expected


def test_clusters_split_where_the_terminal_draws() -> None:
    assert list(grapheme_clusters("a🇯🇵b")) == ["a", "🇯🇵", "b"]
    assert list(grapheme_clusters("👍🏽!")) == ["👍🏽", "!"]
    assert list(grapheme_clusters("éx")) == ["é", "x"]


def test_tabs_have_one_agreed_width() -> None:
    assert cell_width("a\tb") == 1 + 3 + 1


def test_truncation_never_splits_a_glyph() -> None:
    """A wide glyph that would straddle the edge is dropped, not half-drawn."""
    assert truncate_to_width("日本語", 5) == "日本"
    assert cell_width(truncate_to_width("日本語", 5)) == 4
    assert truncate_to_width("👨‍👩‍👧‍👦x", 2) == "👨‍👩‍👧‍👦"


def test_truncation_keeps_the_styling_of_what_survives() -> None:
    styled = "\x1b[31mred\x1b[39m and plain"
    cut = truncate_to_width(styled, 3)
    assert strip_ansi(cut) == "red"
    assert "\x1b[31m" in cut


@pytest.mark.parametrize("width", [0, 1, 5, 40])
def test_padding_lands_on_exactly_the_width(width: int) -> None:
    for text in ("", "hi", "日本語", "\x1b[31mred\x1b[39m", "👍🏽 ok"):
        assert cell_width(pad_to_width(text, width)) == width


def test_padding_truncates_rather_than_letting_a_line_wrap() -> None:
    """One line wider than the terminal pushes every line below it down."""
    assert pad_to_width("far too long", 4) == "far "
