"""Selecting transcript text with the mouse, and getting it onto the clipboard.

Three separate things had to be true for a drag-and-copy to work, and none of
them were: the blocks had to be able to hand back their drawn text, ctrl+c had
to reach the app rather than the prompt, and the copy had to use something the
terminal actually honours.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.geometry import Offset
from textual.selection import Selection

from hx.config import load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.session import new_session
from hx.providers.fake import FakeProvider, text_turn
from hx.providers.models import ModelRegistry
from hx.tools.registry import ToolRegistry
from hx.tui.app import HXApp

MODEL = "anthropic/claude-sonnet-4.5"


def build_app(tmp_path: Path) -> HXApp:
    bus = EventBus()
    models = ModelRegistry()
    session = new_session(tmp_path, MODEL)
    session.set_title("selection session")
    loop = AgentLoop(
        provider=FakeProvider([text_turn("hello there")]),
        session=session,
        tools=ToolRegistry(),
        permissions=None,
        context=ContextBuilder("sys", tmp_path),
        compactor=None,
        injections=InjectionRegistry(),
        bus=bus,
        settings=load_settings(tmp_path),
        model_info=models.get_or_default(MODEL),
    )
    return HXApp(loop, bus, load_settings(tmp_path), models=models)


async def test_a_selection_over_assistant_prose_yields_its_text(
    hx_home: Path, tmp_path: Path
) -> None:
    """Textual can only extract text from a widget whose visual is a ``Text``.

    These blocks render Markdown inside a ``Padding``, so dragging over them
    highlighted the text and copied an empty string.
    """
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        transcript = app._transcript
        transcript.start_assistant_message()
        transcript.append_delta("the quick brown fox")
        await pilot.pause()

        block = transcript._current
        assert block is not None
        assert "the quick brown fox" in (block.rendered_text() or "")
        assert app.screen.get_selected_text() is None

        selected, ending = block.get_selection(Selection(None, None))
        assert "the quick brown fox" in selected
        assert ending == "\n"


async def test_a_selection_over_a_tool_block_yields_its_text(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        transcript = app._transcript
        transcript.add_tool_block("a", "Bash", {"command": "pytest -q"})
        await pilot.pause()

        block = transcript._tools["a"]
        assert "pytest -q" in (block.rendered_text() or "")


async def test_a_copied_line_does_not_arrive_padded_to_the_pane_width(
    hx_home: Path, tmp_path: Path
) -> None:
    """Rich pads every strip out to the full width; a copied paragraph should
    not come with a hundred trailing spaces on each line."""
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        transcript = app._transcript
        transcript.start_assistant_message()
        transcript.append_delta("short")
        await pilot.pause()

        rendered = transcript._current.rendered_text() or ""
        assert rendered.splitlines() == [line.rstrip() for line in rendered.splitlines()]


async def test_ctrl_c_copies_the_selection_and_still_clears_the_prompt(
    hx_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt is a TextArea that binds ctrl+c and holds focus all session,
    so a transcript selection was never what the key copied - and the key still
    has to clear the draft when there is no selection."""
    copied: list[str] = []

    async def record(text: str) -> str:
        copied.append(text)
        return "pbcopy"

    monkeypatch.setattr("hx.tui.clipboard.copy_text", lambda text, **kwargs: record(text))

    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        transcript = app._transcript
        transcript.start_assistant_message()
        transcript.append_delta("selectable prose")
        await pilot.pause()

        # The draft first: Textual drops a selection when a widget's content
        # changes, and the prompt is a widget.
        app._prompt.text = "a draft"
        await pilot.pause()
        app.screen.selections = {transcript._current: Selection(Offset(0, 0), Offset(80, 0))}
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert copied and "selectable prose" in copied[0]
        # The selection was the target, so the draft is untouched.
        assert app._prompt.text == "a draft"

        app.screen.clear_selection()
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert app._prompt.text == ""


async def test_the_clipboard_goes_through_hx_rather_than_osc52_alone(
    hx_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Textual's own copy is OSC 52 only, which macOS Terminal ignores and
    iTerm2 ships disabled - so every copy through it silently did nothing."""
    used: list[str] = []

    async def record(text: str, **kwargs: Any) -> str:
        used.append(text)
        return "pbcopy"

    monkeypatch.setattr("hx.tui.clipboard.copy_text", record)

    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        app.copy_to_clipboard("via textual")
        await pilot.pause()
        await pilot.pause()

        assert used == ["via textual"]


async def test_mouse_reporting_toggles_and_says_when_it_cannot(
    hx_home: Path, tmp_path: Path
) -> None:
    """With reporting on, the terminal's own click-and-drag selection is
    unavailable - which is the escape hatch this command exists to offer."""
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.mouse_reporting is True

        # The headless driver has no mouse support to switch, and says so
        # rather than raising.
        assert app.set_mouse_reporting(False) is False
        assert app.mouse_reporting is True

        await app.submit("/mouse off")
        await pilot.pause()
        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
        assert "no mouse reporting to toggle" in notices

        await app.submit("/mouse sideways")
        await pilot.pause()
        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
        assert "Usage: /mouse [on|off]" in notices
