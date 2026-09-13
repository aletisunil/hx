"""Filtered lists, and the dialogs that used to render with no styling at all."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from hx.term.width import strip_ansi
from hx.tui import paint
from hx.tui.limits import LIST_VISIBLE
from hx.tui.views.dialog import Dialog, Hint, Option, hints_line
from hx.tui.views.login import ConfigureDialog, LoginDialog, ProviderDialog
from hx.tui.views.pickers import (
    DEFAULT_EFFORT_ROW,
    CommandPalette,
    EffortPicker,
    Picker,
    RewindPicker,
    SessionPicker,
)
from tests.term.conftest import assert_lines_fit, plain


@pytest.fixture(autouse=True)
def _pinned_colors() -> None:
    paint.set_color_mode("truecolor")


@dataclass
class FakeCommand:
    name: str
    summary: str


class Letters(Picker):
    title = "Letters"

    def __init__(self, count: int = 5, current: str | None = None) -> None:
        self.count = count
        self.current = current
        super().__init__()

    def rows(self, query: str) -> list[tuple[str, list[str]]]:
        values = [f"item-{index}" for index in range(self.count)]
        return [(v, [v, f"note for {v}"]) for v in values if query in v]

    def current_value(self) -> str | None:
        return self.current


# -- the shared grammar -----------------------------------------------------


def test_every_dialog_is_framed_by_rules() -> None:
    """One border primitive, so two dialogs cannot differ the way #picker and
    #configure did."""
    for component in (
        Letters(),
        ProviderDialog([("a", "Anthropic", True)]),
        LoginDialog(),
        ConfigureDialog("Configure", [("provider", "openai")]),
        Dialog(title="T", options=[Option(label="one")]),
    ):
        lines = plain(component.render(60))
        assert set(lines[0].strip()) == {"─"}
        assert set(lines[-1].strip()) == {"─"}
        assert not any(ch in "".join(lines) for ch in "┌┐└┘├┤")


def test_a_hint_line_is_dim_key_then_muted_description() -> None:
    assert strip_ansi(hints_line([Hint("esc", "cancel"), Hint("enter", "ok")])) == (
        "esc cancel  enter ok"
    )


def test_the_two_markers_answer_two_different_questions() -> None:
    """Where Enter will land, and what is already set. Collapsing them is why
    a row used to signal its highlight three separate ways."""
    picker = Letters(current="item-2")
    lines = [line for line in plain(picker.render(60)) if "item-" in line]
    assert lines[0].strip().startswith("→")
    assert "✓" in lines[2]
    assert "→" not in lines[2]


# -- filtering and moving ---------------------------------------------------


def test_typing_narrows_the_list() -> None:
    picker = Letters(count=12)
    picker.handle_input("text", "item-1")
    shown = [line for line in plain(picker.render(60)) if "item-" in line]
    assert all("item-1" in line for line in shown)


def test_backspace_widens_it_again() -> None:
    picker = Letters(count=12)
    picker.handle_input("text", "item-11")
    picker.handle_input("backspace", "")
    assert any("item-1 " in line or "item-1" in line for line in plain(picker.render(60)))


def test_no_matches_says_so_rather_than_showing_nothing() -> None:
    picker = Letters()
    picker.handle_input("text", "zzz")
    assert any("no matches" in line for line in plain(picker.render(60)))


def test_selection_wraps_in_both_directions() -> None:
    picker = Letters(count=3)
    picker.handle_input("up", "")
    assert picker.selected == 2
    picker.handle_input("down", "")
    assert picker.selected == 0


def test_a_long_list_scrolls_around_the_selection() -> None:
    """So the row Enter will take is always on screen."""
    picker = Letters(count=40)
    for _ in range(20):
        picker.handle_input("down", "")
    shown = [line for line in plain(picker.render(60)) if "item-" in line]
    assert len(shown) == LIST_VISIBLE
    assert any("item-20" in line for line in shown)


def test_a_clipped_list_says_where_you_are_in_it() -> None:
    picker = Letters(count=40)
    assert any("(1/40)" in line for line in plain(picker.render(60)))


def test_enter_returns_the_selected_value_and_escape_returns_nothing() -> None:
    picker = Letters()
    picker.handle_input("down", "")
    picker.handle_input("enter", "")
    assert picker.done and picker.result == "item-1"

    other = Letters()
    other.handle_input("escape", "")
    assert other.done and other.result is None


def test_enter_on_an_empty_list_cancels_rather_than_crashing() -> None:
    picker = Letters()
    picker.handle_input("text", "zzz")
    picker.handle_input("enter", "")
    assert picker.done and picker.result is None


# -- the specific pickers ---------------------------------------------------


def test_the_palette_matches_on_summary_as_well_as_name() -> None:
    """In a palette a user is often looking for a capability, not a name."""
    palette = CommandPalette([FakeCommand("status", "show token cost"), FakeCommand("clear", "x")])
    palette.handle_input("text", "cost")
    rows = [line for line in plain(palette.render(70)) if "/" in line]
    assert any("/status" in line for line in rows)
    assert not any("/clear" in line for line in rows)


def test_a_prefilled_query_stays_visible_and_editable() -> None:
    """`/model opus` should not silently narrow a list whose reason for being
    short the user cannot see."""
    palette = CommandPalette([FakeCommand("status", "s")], initial="stat")
    assert "stat" in " ".join(plain(palette.render(60)))


@dataclass
class FakeInfo:
    id: str = "anthropic/claude-sonnet-4.5"
    reasoning_levels: list[str] = field(default_factory=lambda: ["low", "high"])
    default_reasoning_level: str | None = "low"


def test_effort_offers_only_the_levels_this_model_has() -> None:
    """They differ per model, and a row silently lowered on the way out lied."""
    picker = EffortPicker(FakeInfo(), current=None)
    offered = {value for value, _cells in picker.rows("")}
    assert offered == {DEFAULT_EFFORT_ROW, "low", "high"}
    assert "xhigh" not in offered


def test_the_row_that_clears_the_setting_is_not_a_level_name() -> None:
    """ "none" is a real effort - no reasoning at all - so it cannot double as
    "unset"."""
    picker = EffortPicker(FakeInfo(), current=None)
    assert picker.current_value() == DEFAULT_EFFORT_ROW
    assert DEFAULT_EFFORT_ROW not in FakeInfo().reasoning_levels


@dataclass
class FakeMeta:
    session_id: str
    title: str
    updated_at: float = 1_700_000_000.0
    message_count: int = 31
    prompt_count: int = 1


def test_a_session_is_described_by_prompts_first() -> None:
    """A prompt is a thing the user typed; a message is a wire record, and
    there are about two per call. "31 msgs" describes the protocol."""
    picker = SessionPicker([FakeMeta("abc", "a session")])
    line = next(line for line in plain(picker.render(90)) if "a session" in line)
    assert "1 prompt" in line
    assert line.index("1 prompt") < line.index("31 msgs")


def test_an_old_session_without_prompt_counts_keeps_the_old_shape() -> None:
    picker = SessionPicker([FakeMeta("abc", "old", prompt_count=0)])
    assert "31 msgs" in " ".join(plain(picker.render(90)))


@dataclass
class FakePoint:
    index: int
    text: str
    timestamp: float = 1_700_000_000.0
    compacted: bool = False


def test_rewind_offers_the_newest_first() -> None:
    """A rewind is nearly always undoing the last thing that happened."""
    picker = RewindPicker([FakePoint(0, "first"), FakePoint(1, "second")])
    rows = [line for line in plain(picker.render(70)) if "first" in line or "second" in line]
    assert "second" in rows[0]


def test_a_multiline_prompt_is_collapsed_to_one_row() -> None:
    picker = RewindPicker([FakePoint(0, "line one\nline two\nline three")])
    row = next(line for line in plain(picker.render(70)) if "line one" in line)
    assert "line one line two line three" in row


# -- login and configure ----------------------------------------------------


def test_a_provider_with_a_credential_is_marked() -> None:
    dialog = ProviderDialog([("a", "Anthropic", True), ("o", "OpenAI", False)])
    lines = [line for line in plain(dialog.render(60)) if "Anthropic" in line or "OpenAI" in line]
    assert "✓" in lines[0]
    assert "✓" not in lines[1]


def test_the_login_dialog_can_be_spoken_to_before_it_is_on_screen() -> None:
    """The flow talks first; recording state and repainting is the fix for a
    crash where it reached for a widget that did not exist yet."""
    dialog = LoginDialog()
    dialog.progress("Opening your browser…")
    dialog.show_url("https://example.com/auth", "Finish in the browser:")
    shown = " ".join(plain(dialog.render(70)))
    assert "Finish in the browser" in shown
    assert "example.com/auth" in shown


def test_a_device_code_is_highlighted_and_its_uri_named() -> None:
    dialog = LoginDialog()
    dialog.show_device_code("WXYZ-1234", "https://example.com/device")
    shown = " ".join(plain(dialog.render(70)))
    assert "WXYZ-1234" in shown
    assert "example.com/device" in shown


def test_the_url_is_a_real_hyperlink() -> None:
    dialog = LoginDialog()
    dialog.show_url("https://example.com/auth", "Open this:")
    assert "\x1b]8;;https://example.com/auth\x07" in "".join(dialog.render(70))


def test_an_api_key_is_never_echoed() -> None:
    dialog = ConfigureDialog("Configure", [("provider", "openai")])
    dialog.handle_input("text", "sk-secret-value")
    shown = " ".join(plain(dialog.render(70)))
    assert "sk-secret" not in shown
    assert "•" in shown


def test_configure_returns_what_was_typed() -> None:
    dialog = ConfigureDialog("Configure", [("provider", "openai")])
    dialog.handle_input("text", "sk-abc")
    dialog.handle_input("enter", "")
    assert dialog.done and dialog.result == "sk-abc"


def test_an_empty_key_is_not_saved_as_an_empty_string() -> None:
    dialog = ConfigureDialog("Configure", [])
    dialog.handle_input("enter", "")
    assert dialog.result is None


@pytest.mark.parametrize("width", [30, 50, 80, 140])
def test_they_all_honour_the_renderer_contract(width: int) -> None:
    components: list[Any] = [
        Letters(count=40),
        CommandPalette([FakeCommand("status", "show cost")]),
        SessionPicker([FakeMeta("abc", "a session")]),
        RewindPicker([FakePoint(0, "a prompt")]),
        EffortPicker(FakeInfo(), None),
        ProviderDialog([("a", "Anthropic", True)]),
        LoginDialog(),
        ConfigureDialog("Configure", [("provider", "openai")]),
    ]
    for component in components:
        assert_lines_fit(component, width)
