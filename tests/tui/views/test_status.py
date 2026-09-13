"""The dock: status bar, hints, header."""

from __future__ import annotations

import pytest

from hx.term.width import cell_width, strip_ansi
from hx.tui import paint
from hx.tui.views.status import (
    DANGER_FRACTION,
    METER_CELLS,
    METER_EMPTY,
    METER_FULL,
    METER_MIN_WIDTH,
    WARN_FRACTION,
    Header,
    HintsBar,
    StatusBar,
    justify,
    meter,
)
from tests.term.conftest import assert_lines_fit, plain


@pytest.fixture(autouse=True)
def _pinned_colors() -> None:
    paint.set_color_mode("truecolor")


def loaded() -> StatusBar:
    bar = StatusBar()
    bar.set_location("~/project", "main")
    bar.set_model("anthropic/claude-sonnet-4.5")
    bar.set_context(24_000, 200_000)
    bar.set_tokens(57_000, 3_500)
    bar.set_cache(244_000, 12_000, 0.81)
    bar.set_mode("default", True, "seatbelt")
    bar.update(cost_usd=0.16, latency_ms=1400, effort="high")
    return bar


def test_the_bar_is_two_lines_whatever_the_width() -> None:
    """It is docked; a line appearing or vanishing would shift the transcript."""
    for width in (20, 40, 80, 200):
        assert len(loaded().render(width)) == 2


def test_both_halves_are_shown_when_there_is_room() -> None:
    lines = plain(loaded().render(100))
    assert "~/project (main)" in lines[0]
    assert lines[0].rstrip().endswith("default · sandbox seatbelt")
    assert lines[1].rstrip().endswith("claude-sonnet-4.5 · high")


def test_the_path_is_the_half_that_yields() -> None:
    """The right half is short, fixed, and what a user glances at without
    reading. A long project path must not push it off the end."""
    bar = loaded()
    bar.set_location("~/a/very/deeply/nested/project/path/that/keeps/going", "main")
    line = plain(bar.render(60))[0]
    assert "…" in line
    assert line.rstrip().endswith("default · sandbox seatbelt")


def test_the_right_half_is_dropped_rather_than_crushed() -> None:
    """Below a floor the left half is not worth keeping at all."""
    line = plain(loaded().render(30))[0]
    assert "sandbox" not in line


def test_the_namespace_is_stripped_but_the_route_is_not() -> None:
    """anthropic/ is four wasted columns; (sub) is the only on-screen trace of
    which account a turn was billed to."""
    bar = loaded()
    bar.set_model("openai-codex/gpt-5.6-terra", subscription=True)
    line = plain(bar.render(100))[1]
    assert "openai-codex/" not in line
    assert "gpt-5.6-terra" in line
    assert "(sub)" in line


def test_a_subscription_turn_reports_no_price_rather_than_a_zero() -> None:
    """$0.00 would be a claim about spend rather than the absence of one."""
    bar = loaded()
    bar.set_model("x", subscription=True)
    assert "sub" in plain(bar.render(100))[1]
    assert "$" not in plain(bar.render(100))[1]


@pytest.mark.parametrize(
    ("fraction", "role"),
    [(0.10, "success"), (WARN_FRACTION, "warning"), (DANGER_FRACTION, "error")],
)
def test_the_context_field_is_the_one_thing_that_changes_colour(fraction: float, role: str) -> None:
    """It earns it by having a deadline: it decides whether the next turn
    compacts."""
    bar = loaded()
    bar.set_context(int(200_000 * fraction), 200_000)
    assert paint.color(role).lstrip("#").lower() in _hexes(bar.render(100)[1])


def test_the_context_field_shows_the_gauge_and_what_it_gauges() -> None:
    """The bar for the glance, the counts for when the exact figure matters."""
    bar = loaded()
    bar.set_context(94_000, 1_000_000)
    line = plain(bar.render(120))[1]
    assert "94k/1M" in line
    assert METER_FULL in line and METER_EMPTY in line


@pytest.mark.parametrize(
    ("fraction", "filled"),
    [(0.0, 0), (0.0001, 1), (0.5, METER_CELLS // 2), (1.0, METER_CELLS), (2.0, METER_CELLS)],
)
def test_the_gauge_fills_with_the_fraction_and_never_past_it(fraction: float, filled: int) -> None:
    """A fraction over 1.0 is a bad window figure, not a licence to overdraw."""
    rendered = strip_ansi(meter(fraction, METER_CELLS, "success"))
    assert rendered == METER_FULL * filled + METER_EMPTY * (METER_CELLS - filled)
    assert cell_width(rendered) == METER_CELLS


def test_a_narrow_pane_drops_the_gauge_and_keeps_the_counts() -> None:
    """The bar is the decoration on that field; the numbers are the field."""
    bar = loaded()
    bar.set_context(94_000, 200_000)
    line = plain(bar.render(METER_MIN_WIDTH - 1))[1]
    assert METER_FULL not in line and METER_EMPTY not in line


def _hexes(line: str) -> str:
    """The truecolor escapes in a line, as hex, for comparing against a role."""
    import re

    out = []
    for r, g, b in re.findall(r"\x1b\[38;2;(\d+);(\d+);(\d+)m", line):
        out.append(f"{int(r):02x}{int(g):02x}{int(b):02x}")
    return " ".join(out)


def test_justify_never_exceeds_the_width() -> None:
    for width in range(1, 60):
        assert cell_width(justify("a left half", "a right half", width)) <= width


def test_hints_are_dropped_whole_not_clipped_mid_word() -> None:
    """Half a key name teaches nothing and looks like a rendering fault."""
    bar = HintsBar()
    bar.set_hints([("esc", "interrupt"), ("ctrl+c", "clear"), ("ctrl+p", "commands")])
    narrow = strip_ansi(bar.render(24)[0])
    assert "esc interrupt" in narrow
    assert "comm" not in narrow, "a hint was cut in half"


def test_the_hints_line_is_always_exactly_one_row() -> None:
    bar = HintsBar()
    bar.set_hints([("a", "one"), ("b", "two")])
    for width in (10, 40, 200):
        assert len(bar.render(width)) == 1


def test_the_header_names_the_key_that_expands_it() -> None:
    lines = plain(Header("1.2.3").render(70))
    assert lines[0].strip() == "hx v1.2.3"
    assert "ctrl+o" in lines[-1]


def test_expanding_the_header_lists_every_binding() -> None:
    header = Header("1.2.3")
    collapsed = len(header.render(70))
    header.toggle()
    assert len(header.render(70)) > collapsed


def test_the_expanded_header_aligns_its_columns_by_measuring() -> None:
    """Not by a guessed width, which misaligns the first time something longer
    than the guess appears."""
    header = Header("1.2.3")
    header.toggle()
    # Skip the title; every remaining binding row is "<keys><pad><description>".
    rows = [line for line in plain(header.render(90))[1:] if line.strip()][:8]
    # Where the description starts is the run of spaces after the key column.
    import re

    starts = {match.end() for line in rows if (match := re.match(r"^\s*\S+\s+", line))}
    assert len(starts) == 1, f"descriptions do not share a column: {starts}"


def test_a_quiet_start_draws_no_header() -> None:
    assert Header("1.2.3", quiet=True).render(70) == []


@pytest.mark.parametrize("width", [20, 40, 80, 120])
def test_the_dock_honours_the_renderer_contract(width: int) -> None:
    hints = HintsBar()
    hints.set_hints([("esc", "interrupt"), ("ctrl+c", "clear")])
    for component in (loaded(), hints, Header("1.2.3")):
        assert_lines_fit(component, width)
