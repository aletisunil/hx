"""What comes off text HX did not write.

The rule is one line - untrusted text is plain text - and the cases below are
the ones that matter on a real terminal rather than an exhaustive tour of the
escape grammar.
"""

from __future__ import annotations

import pytest

from hx.term.sanitize import plain_lines, plain_text
from hx.term.screen import CURSOR_MARKER


@pytest.mark.parametrize(
    ("hostile", "expected"),
    [
        # OSC 52 writes the user's clipboard.
        ("\x1b]52;c;cGF5bG9hZA==\x07taken", "taken"),
        # OSC 0 renames their window.
        ("hello\x1b]0;PWNED\x07world", "helloworld"),
        # OSC 8 makes the words a link to somewhere else.
        ("\x1b]8;;http://evil\x07click\x1b]8;;\x07", "click"),
        # The renderer's own cursor marker.
        (f"{CURSOR_MARKER}output", "output"),
        # Cursor movement and erasure desynchronise the differ.
        ("\x1b[2J\x1b[Hgone", "gone"),
        ("\x1b[10A\x1b[5Dup", "up"),
        # Carriage return and backspace overwrite what is already on the line.
        ("real\rfake", "realfake"),
        ("real\x08\x08fake", "realfake"),
        # Colour comes off too: a stray reset inside a tinted block tears it.
        ("\x1b[31mred\x1b[0m", "red"),
        ("\x1b[48;5;22mgreen block\x1b[49m", "green block"),
        # The C1 range is the same attack without the escape byte.
        ("\x9b2Jc1", "2Jc1"),
        ("\x9d52;c;AAA\x07c1 osc", "52;c;AAAc1 osc"),
        ("bell\x07", "bell"),
        # Nothing to remove.
        ("plain text", "plain text"),
        ("wide 日本語 and 👍", "wide 日本語 and 👍"),
    ],
)
def test_what_a_terminal_would_have_acted_on_is_removed(hostile: str, expected: str) -> None:
    assert plain_text(hostile) == expected


def test_an_unterminated_sequence_is_dropped_to_the_end() -> None:
    """Leaving it lets the next chunk of a stream supply the terminator.

    A payload split across two reads would otherwise reassemble itself on the
    terminal after both halves had been passed through as harmless.
    """
    assert plain_text("safe \x1b]52;c;cGF5bG9h") == "safe "
    assert plain_text("safe \x1b[38;5") == "safe "
    assert plain_text("safe \x1b") == "safe "


def test_newlines_are_structure_and_survive() -> None:
    assert plain_text("one\ntwo\nthree") == "one\ntwo\nthree"
    assert plain_lines("one\ntwo") == ["one", "two"]
    assert plain_text("crlf\r\nkept") == "crlf\nkept"


def test_tabs_are_expanded_rather_than_passed_on() -> None:
    """So the width of a line is the same number whoever asks."""
    from hx.term.width import cell_width

    out = plain_text("a\tb")
    assert "\t" not in out
    assert cell_width(out) == len(out)


def test_text_with_nothing_to_remove_is_returned_unchanged() -> None:
    text = "the overwhelming majority of a transcript"
    assert plain_text(text) is text
