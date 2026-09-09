"""The prompt frame: a rule that carries the working status and the scroll state."""

from __future__ import annotations

from hx.tui.widgets.frame import BottomRule
from hx.tui.widgets.hints import HintsBar
from hx.tui.widgets.working import WorkingIndicator


def _busy(label: str = "thinking") -> WorkingIndicator:
    indicator = WorkingIndicator()
    indicator.start(label)
    return indicator


def test_an_idle_rule_is_just_a_rule() -> None:
    line = WorkingIndicator().render_rule(40).plain
    assert line == "─" * 40


def test_a_busy_rule_carries_the_status_inline() -> None:
    line = _busy().render_rule(80).plain
    assert line.startswith("── ")
    assert "Thinking…" in line
    assert "to interrupt" in line
    assert line.endswith("─")
    assert len(line) == 80


def test_a_narrow_rule_degrades_to_the_spinner() -> None:
    """Better a spinner than a status clipped mid-word."""
    line = _busy().render_rule(14).plain
    assert "Thinking" not in line
    assert len(line) == 14


def test_the_rule_shows_what_scrolled_off_the_top() -> None:
    indicator = WorkingIndicator()
    indicator.set_hidden_above(3)
    line = indicator.render_rule(60).plain
    assert "↑ 3 more" in line
    assert len(line) == 60


def test_status_and_overflow_share_the_rule() -> None:
    indicator = _busy()
    indicator.set_hidden_above(7)
    line = indicator.render_rule(90).plain
    assert "Thinking…" in line and "↑ 7 more" in line
    assert len(line) == 90


def test_the_bottom_rule_shows_what_is_below() -> None:
    rule = BottomRule()
    rule.set_hidden_below(4)
    assert "↓ 4 more" in rule.render_rule(40).plain


def test_the_rule_brightens_with_focus() -> None:
    indicator = WorkingIndicator()
    unfocused = indicator.render_rule(20)
    indicator.set_focused_style(True)
    assert indicator.render_rule(20).spans != unfocused.spans


def test_hints_are_dropped_whole_rather_than_clipped() -> None:
    """A key clipped mid-word tells the reader nothing about what to press."""
    bar = HintsBar()
    wide = bar.hints_line(200).plain
    narrow = bar.hints_line(30).plain

    assert len(narrow) <= 30
    assert wide.startswith(narrow.split(" · ")[0])
    assert not narrow.endswith("·")


def test_hints_name_the_keys_that_are_bound() -> None:
    from hx.keys import KEYMAP

    line = HintsBar().hints_line(200).plain
    assert KEYMAP.primary("app.interrupt") in line
    assert KEYMAP.primary("app.message.copy") in line
