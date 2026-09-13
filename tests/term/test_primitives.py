"""The drawing vocabulary, and the invariants every component owes the renderer."""

from __future__ import annotations

import pytest

from hx.term.ansi import fg
from hx.term.component import Component, Container
from hx.term.primitives import (
    CURRENT,
    CURSOR,
    GUTTER,
    Box,
    HangingText,
    LabelledRule,
    Lines,
    Rule,
    Spacer,
    Text,
)
from hx.term.width import cell_width, strip_ansi
from tests.term.conftest import assert_lines_fit, assert_render, plain, render_plain


def test_text_pads_one_column_in_from_each_edge() -> None:
    assert render_plain(Text("hi"), 10) == [" hi"]


def test_text_wraps_inside_its_padding() -> None:
    assert_render(
        Text("the quick brown fox jumps"),
        14,
        """
        | the quick
        | brown fox
        | jumps
        """,
    )


def test_text_with_vertical_padding_gets_blank_rows() -> None:
    assert render_plain(Text("hi", 1, 1), 10) == ["", " hi", ""]


def test_a_rule_is_one_character_repeated() -> None:
    assert render_plain(Rule(), 8) == ["────────"]


def test_a_rule_has_no_corners_at_any_width() -> None:
    """There is no box anywhere in this UI, so there is nothing to misalign."""
    for width in (1, 2, 3, 40, 200):
        line = render_plain(Rule(), width)[0]
        assert set(line) == {"─"}
        assert cell_width(line) == width


def test_a_labelled_rule_sets_the_label_into_the_line() -> None:
    assert render_plain(LabelledRule(label="⠹ Working"), 30) == ["── ⠹ Working ─────────────────"]


def test_a_labelled_rule_can_centre_its_label() -> None:
    line = render_plain(LabelledRule(label="↑ 12 more", align="center"), 31)[0]
    assert line == "────────── ↑ 12 more ──────────"
    assert cell_width(line) == 31


def test_a_label_that_will_not_fit_is_dropped_not_clipped() -> None:
    """A half-drawn status is worse than none; the rule still reads as a frame."""
    assert set(render_plain(LabelledRule(label="a very long status"), 12)[0]) == {"─"}


def test_a_spacer_is_the_only_vertical_separator() -> None:
    assert render_plain(Spacer(2), 10) == ["", ""]


def test_a_hanging_prefix_indents_wrapped_lines_under_the_text() -> None:
    assert_render(
        HangingText("· ", "the model switched because the old one was gone"),
        24,
        """
        | · the model switched
        |   because the old one
        |   was gone
        """,
    )


def test_a_hanging_prefix_indents_explicit_newlines_too() -> None:
    """Most multi-line output is joined strings that never saw the width."""
    assert_render(
        HangingText("· ", "first\nsecond\nthird"),
        20,
        """
        | · first
        |   second
        |   third
        """,
    )


def test_a_box_tints_the_full_width_not_just_the_text() -> None:
    """A block only as wide as its longest line reads as a ragged column."""
    box = Box(1, 1, lambda line: f"\x1b[48;2;52;53;65m{line}\x1b[49m", Text("hi", 0, 0))
    lines = box.render(40)
    assert len(lines) == 3, "one blank row above and below"
    for line in lines:
        assert "\x1b[48;2;52;53;65m" in line
        assert cell_width(line) == 40


def test_a_coloured_span_survives_inside_a_box() -> None:
    box = Box(
        1,
        0,
        lambda line: f"\x1b[48;2;52;53;65m{line}\x1b[49m",
        Text(fg("#ff0000", "red") + " plain", 0, 0),
    )
    assert plain(box.render(30)) == [" red plain"]


def test_lines_passes_rendered_output_through() -> None:
    assert render_plain(Lines(["+1 added", "-2 removed"]), 20) == [" +1 added", " -2 removed"]


def test_a_container_is_concatenation() -> None:
    container = Container(Text("a"), Spacer(1), Text("b"))
    assert render_plain(container, 10) == [" a", "", " b"]


def test_a_clean_child_is_not_redrawn() -> None:
    """This is what makes a spinner beside a long transcript affordable."""
    calls = 0

    class Counted(Text):
        def draw(self, width: int) -> list[str]:
            nonlocal calls
            calls += 1
            return super().draw(width)

    child = Counted("x")
    container = Container(child)
    container.render(40)
    container.render(40)
    assert calls == 1

    child.set_text("y")
    container.render(40)
    assert calls == 2


def test_a_width_change_redraws_everything() -> None:
    """Wrapping changed, so every cached line is wrong."""
    text = Text("the quick brown fox")
    assert len(text.render(12)) > 1
    assert len(text.render(80)) == 1


def test_the_selection_markers_are_the_same_width() -> None:
    """An unselected row has to align under a selected one."""
    assert cell_width(CURSOR) == cell_width(GUTTER) == cell_width(CURRENT) == 2


def _every_component() -> list[tuple[str, Component]]:
    tint = lambda line: f"\x1b[48;2;40;40;50m{line}\x1b[49m"  # noqa: E731
    return [
        ("text", Text("hello world")),
        ("text-wide", Text("日本語のテキストです、これは長い行です")),
        ("text-emoji", Text("👨‍👩‍👧‍👦 family 🇯🇵 flag 👍🏽 thumb")),
        ("text-styled", Text(fg("#ff0000", "red") + " and " + fg("#00ff00", "green"))),
        ("text-padded", Text("padded", 1, 1)),
        ("hanging", HangingText("· ", "a notice that runs on for a while and wraps")),
        ("hanging-wide", HangingText("✓ ", "日本語のテキスト\n二行目です")),
        ("rule", Rule()),
        ("rule-labelled", LabelledRule(label="⠹ Working… (12s · esc to interrupt)")),
        ("rule-centred", LabelledRule(label="↑ 40 more", align="center")),
        ("spacer", Spacer(1)),
        ("lines", Lines(["+  1 added line", "-  2 removed line"])),
        ("box", Box(1, 1, tint, Text("inside a box", 0, 0))),
        ("box-wrapping", Box(1, 1, tint, Text("a much longer line that has to wrap", 0, 0))),
        ("container", Container(Text("a"), Spacer(1), Rule(), Text("b"))),
        ("empty-text", Text("")),
    ]


@pytest.mark.parametrize("width", [1, 2, 3, 8, 20, 40, 80, 100, 200])
@pytest.mark.parametrize(
    ("name", "component"),
    _every_component(),
    ids=lambda v: getattr(v, "__class__", type(v)).__name__ if not isinstance(v, str) else v,
)
def test_every_line_fits_the_width_and_closes_its_styles(
    name: str, component: Component, width: int
) -> None:
    """The renderer refuses to draw a frame that breaks either of these.

    One over-wide line wraps and pushes everything below it down for the rest
    of the session; one unterminated style bleeds into the next line forever.
    """
    assert_lines_fit(component, width)


@pytest.mark.parametrize("width", [8, 40, 100])
def test_no_component_emits_a_stray_escape_in_its_visible_text(width: int) -> None:
    for _name, component in _every_component():
        for line in component.render(width):
            assert "\x1b" not in strip_ansi(line)
