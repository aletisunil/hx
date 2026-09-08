"""Status bar rendering.

The cache and cost fields are the reason the bar exists, so the tests here are
mostly about not losing them.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from textual.geometry import Size

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


def _render(bar: StatusBar, width: int) -> str:
    with patch.object(type(bar), "size", Size(width, 1)):
        return str(bar.render().plain)


def test_wide_bar_shows_every_field() -> None:
    line = _render(_bar(), 140)
    for fragment in ("claude-sonnet-4.5", "98k/128k", "cache", "R21k", "$0.4120", "hx@main"):
        assert fragment in line


@pytest.mark.parametrize("width", [140, 100, 80, 60, 40])
def test_bar_never_overflows_its_width(width: int) -> None:
    assert len(_render(_bar(), width)) <= width


def test_cost_and_mode_outlive_the_optional_fields() -> None:
    """Truncating from the right would drop cost and permission mode first -
    exactly the fields worth keeping."""
    line = _render(_bar(), 80)
    assert "$0.4120" in line
    assert "bypass" in line
    assert "hx@main" not in line


def test_degraded_sandbox_is_called_out() -> None:
    assert "no-sandbox" in _render(_bar(), 140)

    bar = _bar()
    bar.set_mode("default", sandbox_active=True)
    assert "no-sandbox" not in _render(bar, 140)


def test_context_gauge_colour_escalates() -> None:
    bar = _bar()
    bar.set_context(int(0.5 * 128_000), 128_000)
    assert "green" in str(bar._context_field().spans)

    bar.set_context(int((WARN_FRACTION + 0.01) * 128_000), 128_000)
    assert "yellow" in str(bar._context_field().spans)

    bar.set_context(int((DANGER_FRACTION + 0.01) * 128_000), 128_000)
    assert "red" in str(bar._context_field().spans)


def test_cache_field_is_explicit_when_nothing_is_cached() -> None:
    bar = StatusBar()
    assert bar._cache_field().plain == "cache -"
