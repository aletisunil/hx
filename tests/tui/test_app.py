"""TUI behaviour, driven through Textual's Pilot."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static

from hx.config import load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.messages import StopReason
from hx.core.session import new_session
from hx.core.usage import TurnUsage
from hx.providers.base import StreamDelta, StreamEnd
from hx.providers.fake import FakeProvider, text_turn
from hx.providers.models import ModelRegistry
from hx.tools.registry import ToolRegistry
from hx.tui.app import HXApp
from hx.tui.widgets.input import PromptInput
from hx.tui.widgets.statusbar import StatusBar
from hx.tui.widgets.transcript import Transcript

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


async def test_escape_cancels_a_streaming_turn(hx_home: Path, tmp_path: Path) -> None:
    class SlowProvider:
        name = "slow"

        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def astream(self, request: Any) -> Any:
            yield StreamDelta(text="thinking…")
            await asyncio.sleep(30)
            yield StreamEnd(stop_reason=StopReason.END_TURN)

        async def aclose(self) -> None:
            return None

    app = build_app(tmp_path)
    app.loop.provider = SlowProvider()

    async with app.run_test() as pilot:
        await app.submit("go")
        await pilot.pause(0.1)
        await app.action_interrupt()
        for _ in range(30):
            await pilot.pause(0.02)
            notices = " ".join(str(n.render()) for n in app.query_one(Transcript).query("Notice"))
            if "interrupted" in notices:
                break
        assert "interrupted" in notices


async def test_permission_modal_shows_the_diff_before_approval(
    hx_home: Path, tmp_path: Path
) -> None:
    """An approval prompt that hides what it is approving is not consent."""
    from hx.permissions.engine import PermissionRequest
    from hx.tui.widgets.permission import PermissionModal

    diff = "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-old line\n+new line\n"
    request = PermissionRequest(
        tool_name="Edit",
        specifier="a.py",
        params={},
        mutating=True,
        description="Edit(a.py)",
        detail=diff,
    )

    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        app.push_screen(PermissionModal(request))
        await pilot.pause()
        rendered = str(app.screen.query_one("#permission-detail").query_one(Static).render())

    assert "-old line" in rendered
    assert "+new line" in rendered


async def test_permission_modal_returns_the_chosen_scope(hx_home: Path, tmp_path: Path) -> None:
    from hx.permissions.engine import GrantScope, PermissionRequest
    from hx.tui.widgets.permission import PermissionModal

    request = PermissionRequest(
        "Bash", "rm -rf build", {}, True, "Bash(rm -rf build)", "rm -rf build"
    )
    app = build_app(tmp_path)
    answers: list[Any] = []

    async with app.run_test() as pilot:
        app.push_screen(PermissionModal(request), callback=answers.append)
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()

    assert answers[0].allowed
    assert answers[0].scope is GrantScope.ALWAYS


async def test_status_bar_marks_a_degraded_sandbox(hx_home: Path, tmp_path: Path) -> None:
    """The user must never believe they are sandboxed when they are not."""
    app = build_app(tmp_path)
    app.sandbox_active = False

    async with app.run_test() as pilot:
        await pilot.pause()
        assert "no-sandbox" in str(app.query_one(StatusBar).render())


async def test_the_ui_still_works_while_a_modal_is_open(hx_home: Path, tmp_path: Path) -> None:
    """query_one resolves against the *active* screen, so a widget lookup made
    while a permission modal is up would raise and kill whichever worker made
    it. The main widgets are bound once at mount instead."""
    from hx.permissions.engine import PermissionRequest
    from hx.tui.widgets.permission import PermissionModal

    request = PermissionRequest(
        "Bash", "rm -rf build", {}, True, "Bash(rm -rf build)", "rm -rf build"
    )
    app = build_app(tmp_path)

    async with app.run_test() as pilot:
        app.push_screen(PermissionModal(request))
        await pilot.pause()
        assert app.screen is not app.screen_stack[0]

        # Every one of these would have raised NoMatches before.
        app.notice("still reachable")
        app.query_one_status().set_busy(True, "working")
        await app.action_toggle_todos()
        await pilot.pause()

        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
        assert "still reachable" in notices
        assert app._status.busy


async def test_a_subagent_prompt_names_who_is_asking(hx_home: Path, tmp_path: Path) -> None:
    """An approval modal with no visible origin is not an informed approval."""
    from hx.permissions.engine import PermissionRequest
    from hx.tui.widgets.permission import PermissionModal

    request = PermissionRequest(
        "Bash",
        "rm -rf build",
        {},
        True,
        "Bash(rm -rf build)",
        "rm -rf build",
        origin="explore subagent",
    )
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        app.push_screen(PermissionModal(request, origin=request.origin))
        await pilot.pause()
        title = str(app.screen.query_one("#permission-title", Static).render())

    assert "explore subagent" in title
