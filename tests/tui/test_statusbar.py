"""Status bar rendering.

The bar is two lines - location/mode above, stats/model below - each justified
to the pane width. The cache and cost fields are the reason it exists, so the
tests are mostly about not losing them.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from rich.console import Console
from textual.geometry import Size

from hx.tui.theme import THEME
from hx.tui.widgets.statusbar import DANGER_FRACTION, WARN_FRACTION, StatusBar


def _bar() -> StatusBar:
    bar = StatusBar()
    bar.model = "anthropic/claude-sonnet-4.5"
    bar.set_context(98_000, 128_000)
    bar.set_tokens(2_300, 172)
    bar.set_cache(21_000, 2_100, 0.91)
    bar.set_cost(0.412)
    bar.set_latency(2_300)
    bar.set_mode("bypass", sandbox_active=False)
    bar.set_location("hx", "main")
    return bar


def _rendered_colours(renderable: object) -> set[str]:
    """Colours Rich actually paints.

    Asserting on ``Text.spans`` is brittle: the same colour lands on a span or
    on the base style depending on how the Text was built. Rendering is what
    the user sees, so that is what is checked.
    """
    console = Console(width=80, color_system="truecolor", file=io.StringIO())
    colours = set()
    for segment in console.render(renderable):
        if segment.style is not None and segment.style.color is not None:
            colours.add(segment.style.color.name)
    return colours


def _lines(bar: StatusBar, width: int) -> list[str]:
    with patch.object(type(bar), "size", Size(width, 2)):
        return str(bar.render().plain).splitlines()


def test_wide_bar_shows_every_field() -> None:
    text = " ".join(_lines(_bar(), 140))
    for fragment in ("claude-sonnet-4.5", "77%/128k", "R21k", "CH91%", "$0.4120", "hx (main)"):
        assert fragment in text


def test_the_bar_is_two_lines() -> None:
    assert len(_lines(_bar(), 140)) == 2


@pytest.mark.parametrize("width", [140, 100, 80, 60, 40])
def test_no_line_overflows_the_pane(width: int) -> None:
    """An overflowing line wraps and pushes the footer off the bottom."""
    for line in _lines(_bar(), width):
        assert len(line) <= width


def test_the_left_half_survives_a_narrow_pane() -> None:
    """Left is flush and yields last, so the numbers outlive the decoration."""
    stats = _lines(_bar(), 60)[1]
    assert "$0.4120" in stats


def test_degraded_sandbox_is_called_out() -> None:
    assert "no-sandbox" in " ".join(_lines(_bar(), 140)).lower()

    bar = _bar()
    bar.set_mode("default", sandbox_active=True, backend="seatbelt")
    text = " ".join(_lines(bar, 140))
    assert "sandbox seatbelt" in text
    assert "no-sandbox" not in text.lower()


def test_context_gauge_colour_escalates() -> None:
    """Asserted against theme roles, not literal colours - the palette owns the
    hex, so a theme switch must not break the test."""
    bar = _bar()
    bar.set_context(int(0.5 * 128_000), 128_000)
    assert THEME.fg("success") in _rendered_colours(bar._context_field())

    bar.set_context(int((WARN_FRACTION + 0.01) * 128_000), 128_000)
    assert THEME.fg("warning") in _rendered_colours(bar._context_field())

    bar.set_context(int((DANGER_FRACTION + 0.01) * 128_000), 128_000)
    assert THEME.fg("error") in _rendered_colours(bar._context_field())


def test_cache_field_is_explicit_when_nothing_is_cached() -> None:
    assert StatusBar()._cache_field().plain == "cache -"


def test_the_palette_drives_the_colours() -> None:
    """Switching theme must repaint the bar, not leave half of it dark-themed."""
    bar = _bar()
    THEME.use("dark")
    dark = _rendered_colours(bar._context_field())
    THEME.use("light")
    light = _rendered_colours(bar._context_field())
    assert dark != light


def test_a_long_path_never_pushes_the_mode_off_the_bar() -> None:
    """The working directory is long and expendable; the permission mode is
    short and safety-critical. The path must be what truncates."""
    bar = _bar()
    bar.set_location("/very/deeply/nested/project/path/that/goes/on/and/on/forever", "main")

    top = _lines(bar, 60)[0]
    assert "bypass" in top
    assert "no-sandbox" in top
    assert "…" in top, "the path should be the thing that gets clipped"


def test_a_long_model_id_still_leaves_the_stats_readable() -> None:
    bar = _bar()
    bar.model = "some-vendor/an-extremely-long-experimental-model-name-preview"
    stats = _lines(bar, 60)[1]
    assert len(stats) <= 60


def test_a_subscription_model_is_tagged_rather_than_priced() -> None:
    """Which credential paid for a turn has to be readable off the bar: the
    stripped model name is otherwise the only trace of the route, and it is
    gone."""
    bar = _bar()
    bar.set_model("openai-codex/gpt-5.6-terra", subscription=True)

    text = " ".join(_lines(bar, 140))
    assert "gpt-5.6-terra (sub)" in text


def test_the_reasoning_depth_rides_next_to_the_model() -> None:
    """It is resolved per model, so switching models can change it without
    anyone typing anything."""
    bar = _bar()
    bar.set_model("openai-codex/gpt-5.6-terra", subscription=True)
    bar.set_effort("high")

    assert "gpt-5.6-terra (sub) · high" in " ".join(_lines(bar, 140))

    # A route with no say in it says nothing rather than claiming a depth.
    bar.set_effort(None)
    assert "· high" not in " ".join(_lines(bar, 140))


def test_a_bar_with_no_model_yet_still_renders() -> None:
    bar = _bar()
    bar.set_model("", subscription=False)
    assert "no model" in " ".join(_lines(bar, 140))
