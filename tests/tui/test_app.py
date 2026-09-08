"""TUI behaviour, driven through Textual's Pilot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hx.config import load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.session import new_session
from hx.core.usage import TurnUsage
from hx.providers.fake import FakeProvider, text_turn
from hx.providers.models import ModelRegistry
from hx.tools.registry import ToolRegistry
from hx.tui.app import HXApp
from hx.tui.widgets.input import PromptInput
from hx.tui.widgets.statusbar import StatusBar
from hx.tui.widgets.transcript import Transcript
from tests.conftest import unimplemented

MODEL = "anthropic/claude-sonnet-4.5"


def build_app(tmp_path: Path, script: list[Any] | None = None) -> HXApp:
    bus = EventBus()
    models = ModelRegistry()
    loop = AgentLoop(
        provider=FakeProvider(script if script is not None else [text_turn("hello there")]),
        session=new_session(tmp_path, MODEL),
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


async def test_app_boots_and_shows_the_status_bar(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.query_one(StatusBar)
        assert status.model == MODEL
        assert status.context_window > 0
        assert "claude-sonnet-4.5" in str(status.render())


async def test_a_turn_streams_into_the_transcript(hx_home: Path, tmp_path: Path) -> None:
    usage = TurnUsage(input_tokens=120, output_tokens=8, cache_read_tokens=880, cost_usd=0.004)
    app = build_app(tmp_path, [text_turn("hello there", usage=usage)])
    async with app.run_test() as pilot:
        await app.submit("hi")
        await pilot.pause()
        for _ in range(20):
            await pilot.pause(0.02)
            if app.query_one(StatusBar).cost_usd:
                break

        status = app.query_one(StatusBar)
        assert status.cache_read == 880
        assert status.cost_usd == pytest.approx(0.004)
        rendered = str(app.query_one(StatusBar).render())
        assert "cache" in rendered
        assert app.query_one(Transcript).query("MessageBlock")


async def test_status_bar_reports_cache_and_cost_after_a_turn(
    hx_home: Path, tmp_path: Path
) -> None:
    usage = TurnUsage(input_tokens=100, output_tokens=10, cache_read_tokens=900, cost_usd=0.01)
    app = build_app(tmp_path, [text_turn("done", usage=usage)])
    async with app.run_test() as pilot:
        await app.submit("go")
        for _ in range(20):
            await pilot.pause(0.02)
            if app.query_one(StatusBar).cache_read:
                break
        assert app.query_one(StatusBar).cache_hit_rate == pytest.approx(0.9)


async def test_unknown_slash_command_is_reported_not_sent_to_the_model(
    hx_home: Path, tmp_path: Path
) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await app.submit("/nonsense")
        await pilot.pause()
        assert not app.loop.provider.requests
        assert app.query_one(Transcript).query("Notice")


async def test_help_lists_only_implemented_commands(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await app.submit("/help")
        await pilot.pause()
        text = " ".join(str(n.render()) for n in app.query_one(Transcript).query("Notice"))
        assert "/model" in text and "/cost" in text


async def test_shift_tab_cycles_permission_mode(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        before = app.mode
        await app.action_cycle_mode()
        await pilot.pause()
        assert app.mode != before
        assert app.query_one(StatusBar).mode == app.mode.value


async def test_enter_submits_and_ctrl_j_inserts_a_newline(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.pause()
        prompt.text = "line one"
        await pilot.press("ctrl+j")
        assert "\n" in prompt.text
        await pilot.press("enter")
        await pilot.pause()
        assert prompt.text == ""


@unimplemented
async def test_escape_cancels_a_streaming_turn() -> None:
    raise NotImplementedError


@unimplemented
async def test_permission_modal_shows_the_diff_before_approval() -> None:
    """An approval prompt that hides what it is approving is not consent."""
    raise NotImplementedError


@unimplemented
async def test_status_bar_marks_a_degraded_sandbox() -> None:
    raise NotImplementedError
