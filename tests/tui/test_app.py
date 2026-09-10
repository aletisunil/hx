"""TUI behaviour, driven through Textual's Pilot."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static

from hx.config import load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.messages import StopReason
from hx.core.session import load_session, new_session
from hx.core.usage import TurnUsage
from hx.providers.base import StreamDelta, StreamEnd
from hx.providers.fake import FakeProvider, text_turn
from hx.providers.models import ModelRegistry
from hx.tools.registry import ToolRegistry
from hx.tui.app import HXApp
from hx.tui.commands import Command
from hx.tui.widgets.input import PromptInput
from hx.tui.widgets.statusbar import StatusBar
from hx.tui.widgets.transcript import Transcript

MODEL = "anthropic/claude-sonnet-4.5"


def _gpt5() -> Any:
    """A second catalogue entry, so ``/model`` has something to switch to."""
    from hx.providers.models import CacheMode, ModelInfo, ModelPricing

    return ModelInfo(
        id="openai/gpt-5",
        name="GPT-5",
        context_window=400_000,
        max_output_tokens=8192,
        pricing=ModelPricing(prompt=1e-6, completion=2e-6),
        cache_mode=CacheMode.IMPLICIT,
    )


def build_app(tmp_path: Path, script: list[Any] | None = None) -> HXApp:
    bus = EventBus()
    models = ModelRegistry()
    session = new_session(tmp_path, MODEL)
    # Already named, so a turn here is one provider call and not two - session
    # naming has its own tests.
    session.set_title("app session")
    loop = AgentLoop(
        provider=FakeProvider(script if script is not None else [text_turn("hello there")]),
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
        assert "R880" in rendered and "CH" in rendered
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


async def _stream(app: HXApp, pilot: Any, count: int, tag: str = "") -> None:
    transcript = app.query_one(Transcript)
    for index in range(count):
        transcript.append_delta(f"{tag}word{index} " + ("\n\n" if index % 10 == 9 else ""))
        await pilot.pause()


async def test_the_transcript_keeps_following_the_tail_after_the_layout_moves(
    hx_home: Path, tmp_path: Path
) -> None:
    """Opening the sidebar or resizing must not strand the reader mid-answer.

    Both make the transcript shorter without moving the scroll offset. Gating
    the follow on "is the offset at the bottom?" answered no from then on, so
    the rest of the answer streamed off-screen.
    """
    app = build_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        transcript = app.query_one(Transcript)
        transcript.add_user_message("go")
        transcript.start_assistant_message()
        await _stream(app, pilot, 200)
        assert transcript.scroll_offset.y == transcript.max_scroll_y > 0

        await app.action_todos_toggle()
        await pilot.pause()
        await _stream(app, pilot, 40, "post")
        assert transcript.scroll_offset.y == transcript.max_scroll_y

        await pilot.resize_terminal(72, 24)
        await pilot.pause()
        await _stream(app, pilot, 40, "resized")
        assert transcript.scroll_offset.y == transcript.max_scroll_y


async def test_reading_back_is_not_yanked_to_the_tail(hx_home: Path, tmp_path: Path) -> None:
    """The other half of the bargain: scrolling up holds while a turn streams."""
    app = build_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        transcript = app.query_one(Transcript)
        transcript.add_user_message("go")
        transcript.start_assistant_message()
        await _stream(app, pilot, 200)

        transcript.page_up()
        await pilot.pause()
        parked = transcript.scroll_offset.y
        assert parked < transcript.max_scroll_y
        await _stream(app, pilot, 60, "more")
        assert transcript.scroll_offset.y == parked

        transcript.scroll_to_bottom()
        await pilot.pause()
        await _stream(app, pilot, 20, "tail")
        assert transcript.scroll_offset.y == transcript.max_scroll_y


async def test_a_failed_tool_call_shows_its_reason_in_the_block(
    hx_home: Path, tmp_path: Path
) -> None:
    """The loop carries the failure text to the block that drew the red band."""
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one(Transcript)
        transcript.add_tool_block("t1", "Edit", {"file_path": "app.py"})
        transcript.finish_tool_block("t1", "error", True, detail="String to replace was not found")
        await pilot.pause()
        block = transcript._tools["t1"]
        assert "String to replace was not found" in block.output


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
        await app.action_mode_cycle()
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


async def test_prompt_stays_editable_and_queues_while_streaming(
    hx_home: Path, tmp_path: Path
) -> None:
    class QueuedProvider:
        name = "queued"

        def __init__(self) -> None:
            self.requests: list[Any] = []
            self.release_first = asyncio.Event()

        async def astream(self, request: Any) -> Any:
            self.requests.append(request)
            if len(self.requests) == 1:
                yield StreamDelta(text="working…")
                await self.release_first.wait()
            else:
                yield StreamDelta(text="follow-up done")
            yield StreamEnd(stop_reason=StopReason.END_TURN)

        async def aclose(self) -> None:
            return None

    app = build_app(tmp_path)
    provider = QueuedProvider()
    app.loop.provider = provider

    async with app.run_test() as pilot:
        await app.submit("first")
        await pilot.pause(0.1)

        prompt = app.query_one(PromptInput)
        assert not prompt.read_only
        for key in ("f", "o", "l", "l", "o", "w", "space", "u", "p"):
            await pilot.press(key)
        assert prompt.text == "follow up"

        await pilot.press("enter")
        await pilot.pause()
        assert prompt.text == ""
        assert app._queued == ["follow up"]
        assert len(provider.requests) == 1

        provider.release_first.set()
        for _ in range(30):
            await pilot.pause(0.02)
            if len(provider.requests) == 2:
                break

        assert len(provider.requests) == 2
        user_messages = [
            message.text() for message in app.loop.session.messages if message.role == "user"
        ]
        assert user_messages == ["first", "follow up"]


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

    # Numbered the way the transcript numbers a diff, so the line being approved
    # is the line the user will later see changed.
    assert "-    1 old line" in rendered
    assert "+    1 new line" in rendered


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
        app.query_one_status().set_mode(app.mode.value, sandbox_active=False)
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
        app._working.start("thinking")
        await app.action_todos_toggle()
        await pilot.pause()

        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
        assert "still reachable" in notices
        assert app._working.busy


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


async def test_every_planned_command_is_registered(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    registered = {command.name for command in app.commands.all()}
    planned = {
        "model",
        "models",
        "clear",
        "compact",
        "resume",
        "cost",
        "context",
        "permissions",
        "mode",
        "skills",
        "mcp",
        "agents",
        "todos",
        "rewind",
        "init",
        "help",
        "quit",
    }
    assert planned <= registered


async def test_rewind_redraws_the_transcript_and_hands_the_prompt_back(
    hx_home: Path, tmp_path: Path
) -> None:
    """A rewind is nearly always the first half of "say that differently", so
    the prompt that was cut goes back in the box rather than being lost."""
    app = build_app(tmp_path, [text_turn("first answer")])
    async with app.run_test() as pilot:
        await app.submit("first prompt")
        await pilot.pause()
        for _ in range(20):
            await pilot.pause(0.02)
            if len(app.loop.session.messages) >= 2:
                break

        point = app.loop.session.rewind_points()[-1]
        app.rewind_to(point.index)
        await pilot.pause()

        assert app.loop.session.active_messages() == []
        assert not app.query_one(Transcript).query("MessageBlock")
        assert app.query_one(PromptInput).text == "first prompt"


async def test_rewind_is_refused_while_a_turn_is_running(hx_home: Path, tmp_path: Path) -> None:
    """Cutting the transcript out from under a streaming turn would leave the
    reply landing in a session that no longer has its prompt."""
    app = build_app(tmp_path, [text_turn("answer")])
    async with app.run_test() as pilot:
        await app.submit("a prompt")
        app._turn_worker = _Unfinished()
        await app.submit("/rewind")
        await pilot.pause()

        assert "Interrupt the running turn" in _last_notice(app)


class _Unfinished:
    """A worker that never finishes, so ``is_busy`` stays true."""

    is_finished = False


def _last_notice(app: HXApp) -> str:
    return " ".join(str(node.render()) for node in app.query_one(Transcript).query(Static))


async def test_mode_command_changes_the_permission_mode(hx_home: Path, tmp_path: Path) -> None:
    from hx.config import PermissionMode

    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await app.submit("/mode plan")
        await pilot.pause()

        assert app.mode is PermissionMode.PLAN
        assert app.query_one_status().mode == "plan"

        await app.submit("/mode nonsense")
        await pilot.pause()
        assert app.mode is PermissionMode.PLAN, "an invalid mode must not change anything"


async def test_permissions_command_reports_the_sandbox_state(hx_home: Path, tmp_path: Path) -> None:
    """A user reading /permissions must not be misled about what protects them."""
    from hx.config import PermissionMode
    from hx.permissions.engine import Decision, PermissionEngine, parse_rule

    app = build_app(tmp_path)
    app.sandbox_active = False
    app.loop.permissions = PermissionEngine(
        PermissionMode.DEFAULT, [parse_rule("Bash(rm:*)", "settings", Decision.DENY)], tmp_path
    )

    async with app.run_test() as pilot:
        await app.submit("/permissions")
        await pilot.pause()
        text = " ".join(str(n.render()) for n in app._transcript.query("Notice"))

    assert "Sandbox: none" in text
    assert "NOT confined" in text
    assert "deny  Bash(rm:*)" in text


async def test_bang_runs_a_shell_command_without_a_model_turn(
    hx_home: Path, tmp_path: Path
) -> None:
    from hx.tools.bash import BackgroundJobs, BashTool, PersistentShell

    shell = PersistentShell(tmp_path)
    await shell.start()
    app = build_app(tmp_path)
    app.loop.tools.register(BashTool(shell, BackgroundJobs(tmp_path / "logs")))

    try:
        async with app.run_test() as pilot:
            await app.submit("!echo passthrough-ok")
            for _ in range(40):
                await pilot.pause(0.05)
                blocks = app._transcript.query("ToolBlock")
                if blocks and blocks[0].summary:
                    break

            assert "passthrough-ok" in blocks[0].output
            assert not app.loop.provider.requests, "! must not spend a model turn"
    finally:
        await shell.close()


async def test_bang_still_goes_through_the_permission_engine(hx_home: Path, tmp_path: Path) -> None:
    """A shortcut that skipped permissions would be a hole in both layers."""
    from hx.config import PermissionMode
    from hx.permissions.engine import Decision, PermissionEngine, parse_rule
    from hx.tools.bash import BackgroundJobs, BashTool, PersistentShell

    shell = PersistentShell(tmp_path)
    await shell.start()
    app = build_app(tmp_path)
    app.loop.tools.register(BashTool(shell, BackgroundJobs(tmp_path / "logs")))
    app.loop.permissions = PermissionEngine(
        PermissionMode.DEFAULT, [parse_rule("Bash(rm:*)", "t", Decision.DENY)], tmp_path
    )

    try:
        async with app.run_test() as pilot:
            await app.submit("!rm -rf something")
            await pilot.pause(0.2)
            text = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
            assert "Refused" in text
    finally:
        await shell.close()


async def test_ctrl_p_opens_the_command_palette(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        app.run_worker(app.action_commands(), name="palette")
        await pilot.pause(0.1)
        assert app.screen is not app.screen_stack[0]
        assert app.screen.query_one("#picker-title", Static)
        await pilot.press("escape")
        await pilot.pause()


async def test_the_theme_setting_reaches_textual(hx_home: Path, tmp_path: Path) -> None:
    """`theme` was accepted by the config layer and then silently ignored."""
    from hx.config import load_settings
    from hx.tui.theme import THEME

    for name in ("light", "dark"):
        settings = load_settings(tmp_path, {"theme": name})
        app = build_app(tmp_path)
        app.settings = settings
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.theme == f"hx-{name}"
            assert THEME.palette.name == name


async def test_model_command_switches_the_model(hx_home: Path, tmp_path: Path) -> None:
    """/model with an unambiguous query switches without opening the picker."""
    from hx.providers.models import CacheMode, ModelInfo, ModelPricing

    app = build_app(tmp_path)
    app.models._models = {
        "openai/gpt-5": ModelInfo(
            id="openai/gpt-5",
            name="GPT-5",
            context_window=400_000,
            max_output_tokens=8192,
            pricing=ModelPricing(prompt=1e-6, completion=2e-6),
            cache_mode=CacheMode.IMPLICIT,
        ),
        MODEL: app.models.get_or_default(MODEL),
    }

    async with app.run_test() as pilot:
        await app.submit("/model gpt-5")
        await pilot.pause()

        assert app.loop.model == "openai/gpt-5"
        assert app.loop.session.meta.model == "openai/gpt-5"
        assert app.query_one_status().model == "openai/gpt-5"
        assert app.query_one_status().context_window == 400_000


async def test_model_command_persists_the_choice_for_the_next_session(
    hx_home: Path, tmp_path: Path
) -> None:
    """The switch outlives the session, and unrelated settings survive the write."""
    import json

    from hx.paths import user_settings_file

    settings_file = user_settings_file()
    settings_file.write_text(json.dumps({"theme": "light", "models": {"max_tokens": 4096}}))

    app = build_app(tmp_path)
    app.models._models = {"openai/gpt-5": _gpt5(), MODEL: app.models.get_or_default(MODEL)}

    async with app.run_test() as pilot:
        await app.submit("/model gpt-5")
        await pilot.pause()

    saved = json.loads(settings_file.read_text())
    assert saved["models"]["model"] == "openai/gpt-5"
    assert saved["models"]["max_tokens"] == 4096
    assert saved["theme"] == "light"
    assert load_settings(tmp_path).models.model == "openai/gpt-5"


async def test_model_command_survives_an_unwritable_settings_file(
    hx_home: Path, tmp_path: Path
) -> None:
    """A broken settings file warns; it must not undo the in-session switch."""
    from hx.paths import user_settings_file

    app = build_app(tmp_path)
    app.models._models = {"openai/gpt-5": _gpt5(), MODEL: app.models.get_or_default(MODEL)}

    # Corrupted after startup - load_settings would refuse to boot the app otherwise.
    user_settings_file().write_text("{ not json")

    async with app.run_test() as pilot:
        await app.submit("/model gpt-5")
        await pilot.pause()

        assert app.loop.model == "openai/gpt-5"
        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
        assert "Could not save the model choice" in notices


async def test_model_command_warns_when_the_project_pins_the_model(
    hx_home: Path, tmp_path: Path
) -> None:
    """A project-level models.model wins next session, so the user is told."""
    import json

    (tmp_path / ".hx").mkdir(exist_ok=True)
    (tmp_path / ".hx" / "settings.json").write_text(
        json.dumps({"models": {"model": "anthropic/claude-opus-4.1"}})
    )

    app = build_app(tmp_path)
    app.models._models = {"openai/gpt-5": _gpt5(), MODEL: app.models.get_or_default(MODEL)}

    async with app.run_test() as pilot:
        await app.submit("/model gpt-5")
        await pilot.pause()

        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
        assert "overrides this on the next start" in notices


async def test_model_command_reports_an_empty_catalogue(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    app.models._models = {}
    async with app.run_test() as pilot:
        await app.submit("/model")
        await pilot.pause()
        text = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
    assert "/models refresh" in text


async def test_model_command_says_why_the_catalogue_is_empty(hx_home: Path, tmp_path: Path) -> None:
    """A remedy is not advice when the remedy is the thing that keeps failing."""
    app = build_app(tmp_path)
    app.models._models = {}
    app.models._loaded = True
    app.models.refresh_error = "TLS certificate verification failed (self signed certificate)."

    async with app.run_test() as pilot:
        await app.submit("/model")
        await pilot.pause()
        text = " ".join(str(n.render()) for n in app._transcript.query("Notice"))

    assert "TLS certificate verification failed" in text


async def test_model_command_opens_the_picker_from_a_submission(
    hx_home: Path, tmp_path: Path
) -> None:
    """An ambiguous /model typed at the prompt has to reach the picker.

    Submissions run outside worker context, where push_screen_wait raises.
    """
    from hx.providers.models import CacheMode, ModelInfo, ModelPricing
    from hx.tui.widgets.palette import ModelPicker

    def info(model_id: str) -> ModelInfo:
        return ModelInfo(
            id=model_id,
            name=model_id,
            context_window=400_000,
            max_output_tokens=8192,
            pricing=ModelPricing(prompt=1e-6, completion=2e-6),
            cache_mode=CacheMode.IMPLICIT,
        )

    app = build_app(tmp_path)
    app.models._models = {
        "openai/gpt-5": info("openai/gpt-5"),
        "openai/gpt-5-mini": info("openai/gpt-5-mini"),
        MODEL: app.models.get_or_default(MODEL),
    }

    async with app.run_test() as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        prompt.text = "/model gpt-5"
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert isinstance(app.screen, ModelPicker)
        await pilot.press("escape")
        await pilot.pause(0.1)

    notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
    assert "failed" not in notices


async def test_model_command_takes_an_id_that_prefixes_others(
    hx_home: Path, tmp_path: Path
) -> None:
    """A full id is a choice, not a query - it must not open the picker."""
    from hx.providers.models import CacheMode, ModelInfo, ModelPricing

    def info(model_id: str) -> ModelInfo:
        return ModelInfo(
            id=model_id,
            name=model_id,
            context_window=400_000,
            max_output_tokens=8192,
            pricing=ModelPricing(prompt=1e-6, completion=2e-6),
            cache_mode=CacheMode.IMPLICIT,
        )

    app = build_app(tmp_path)
    app.models._models = {
        "openai/gpt-5": info("openai/gpt-5"),
        "openai/gpt-5-mini": info("openai/gpt-5-mini"),
        MODEL: app.models.get_or_default(MODEL),
    }

    async with app.run_test() as pilot:
        await app.submit("/model openai/gpt-5")
        await pilot.pause(0.1)

        assert app.screen is app.screen_stack[0], "the picker should not have opened"
        assert app.loop.model == "openai/gpt-5"


async def test_a_failing_command_does_not_kill_the_app(hx_home: Path, tmp_path: Path) -> None:
    """A crash in one command used to take the whole session with it."""

    async def boom(ctx: object, args: str) -> None:
        raise RuntimeError("kaboom")

    app = build_app(tmp_path)
    app.commands.register(Command("boom", "Explodes", boom))

    async with app.run_test() as pilot:
        await app.submit("/boom")
        await pilot.pause(0.1)
        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
        assert app.is_running, "the app should survive a command that raises"

    assert "kaboom" in notices


async def test_exit_is_an_alias_for_quit(hx_home: Path, tmp_path: Path) -> None:
    """Users type /exit; it should quit, not report an unknown command."""
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await app.submit("/exit")
        await pilot.pause(0.1)
        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))

    assert "Unknown command" not in notices
    assert app.commands.get("exit").name == "quit"


async def test_configure_saves_and_applies_a_new_key(hx_home: Path, tmp_path: Path) -> None:
    """A key that can only be set at first run leaves a revoked key unfixable."""
    from hx.providers.openrouter import load_api_key
    from hx.tui.widgets.configure import ConfigureModal

    class FakeProviderWithKey:
        name = "fake"

        def __init__(self) -> None:
            self.api_key = "old"
            self.requests: list[object] = []

        def set_api_key(self, key: str) -> None:
            self.api_key = key

        async def aclose(self) -> None:
            return None

    app = build_app(tmp_path)
    app.loop.provider = FakeProviderWithKey()

    async with app.run_test() as pilot:
        worker = app.run_worker(app.commands.dispatch(app._command_context(), "/configure"))
        await pilot.pause(0.1)

        modal = app.screen
        assert isinstance(modal, ConfigureModal)
        modal.query_one("#configure-input", Input).value = "sk-or-v1-newkey0123456789"
        await pilot.press("enter")
        await pilot.pause(0.1)
        await worker.wait()

        assert app.loop.provider.api_key == "sk-or-v1-newkey0123456789"

        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
        assert "sk-or-…6789" in notices
        assert "newkey0123456789" not in notices, "the key must never reach the transcript"

    assert load_api_key() == "sk-or-v1-newkey0123456789"
    assert (hx_home / "auth.json").stat().st_mode & 0o777 == 0o600


async def test_configure_warns_when_the_environment_wins(
    hx_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saving a key while an env var is set looks like a no-op otherwise."""
    from hx.tui.widgets.configure import ConfigureModal

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-the-environment")
    app = build_app(tmp_path)

    async with app.run_test() as pilot:
        app.run_worker(app.commands.dispatch(app._command_context(), "/configure"))
        await pilot.pause(0.1)

        modal = app.screen
        assert isinstance(modal, ConfigureModal)
        warning = str(modal.query_one("#configure-warning", Static).render())
        assert "OPENROUTER_API_KEY" in warning
        assert "precedence" in warning

        await pilot.press("escape")
        await pilot.pause()


async def test_configure_cancelled_changes_nothing(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        app.run_worker(app.commands.dispatch(app._command_context(), "/configure"))
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.1)
    assert not (hx_home / "auth.json").exists()


def _catalogue(app: HXApp, *model_ids: str) -> None:
    from hx.providers.models import CacheMode, ModelInfo, ModelPricing

    app.models._models = {
        model_id: ModelInfo(
            id=model_id,
            name=model_id,
            context_window=400_000,
            max_output_tokens=8192,
            pricing=ModelPricing(prompt=1e-6, completion=2e-6),
            cache_mode=CacheMode.IMPLICIT,
        )
        for model_id in model_ids
    }


async def test_the_picker_is_navigable_from_the_filter_box(hx_home: Path, tmp_path: Path) -> None:
    """The filter box holds focus, so the arrow keys never reached the list -
    nothing was ever highlighted and Enter picked whatever sorted first."""
    from textual.widgets import OptionList

    from hx.tui.widgets.palette import ModelPicker

    app = build_app(tmp_path)
    _catalogue(app, "openai/gpt-5", "openai/gpt-5-mini", "anthropic/claude-opus-5")

    async with app.run_test() as pilot:
        await app.submit("/model")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ModelPicker)
        options = app.screen.query_one("#picker-options", OptionList)

        assert options.highlighted == 0, "a row must be selected before any key is pressed"
        await pilot.press("down")
        await pilot.pause()
        assert options.highlighted == 1
        await pilot.press("down")
        await pilot.pause()
        assert options.highlighted == 2
        await pilot.press("up")
        await pilot.pause()
        assert options.highlighted == 1

        chosen = options.get_option_at_index(1).id
        await pilot.press("enter")
        await pilot.pause(0.1)

    assert app.loop.model == chosen


async def test_typing_in_the_picker_narrows_and_keeps_a_selection(
    hx_home: Path, tmp_path: Path
) -> None:
    from textual.widgets import OptionList

    app = build_app(tmp_path)
    _catalogue(app, "openai/gpt-5", "openai/gpt-5-mini", "anthropic/claude-opus-5")

    async with app.run_test() as pilot:
        await app.submit("/model")
        await pilot.pause(0.1)
        options = app.screen.query_one("#picker-options", OptionList)
        assert options.option_count == 3

        for char in "opus":
            await pilot.press(char)
        await pilot.pause()

        assert options.option_count == 1
        assert options.highlighted == 0
        await pilot.press("enter")
        await pilot.pause(0.1)

    assert app.loop.model == "anthropic/claude-opus-5"


async def test_a_model_query_pre_fills_the_filter_box(hx_home: Path, tmp_path: Path) -> None:
    """`/model gpt-5` narrows the list; leaving the box empty made that look
    like the filter had failed to apply."""
    from hx.tui.widgets.palette import ModelPicker

    app = build_app(tmp_path)
    _catalogue(app, "openai/gpt-5", "openai/gpt-5-mini", "anthropic/claude-opus-5")

    async with app.run_test() as pilot:
        await app.submit("/model gpt-5")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ModelPicker)
        assert app.screen.query_one("#picker-filter", Input).value == "gpt-5"
        await pilot.press("escape")
        await pilot.pause(0.1)


async def test_a_query_matching_nothing_says_so(hx_home: Path, tmp_path: Path) -> None:
    """Silently falling back to the whole catalogue reads as a broken filter."""
    app = build_app(tmp_path)
    _catalogue(app, "openai/gpt-5", "anthropic/claude-opus-5")

    async with app.run_test() as pilot:
        await app.submit("/model zzzznope")
        await pilot.pause(0.1)
        assert app.screen.query_one("#picker-filter", Input).value == ""
        await pilot.press("escape")
        await pilot.pause(0.1)
        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))

    assert "No model matches" in notices


# -- pi's interaction model ------------------------------------------------


async def test_ctrl_c_clears_the_prompt_and_a_second_press_exits(
    hx_home: Path, tmp_path: Path
) -> None:
    """Escape cancels turns, so ctrl+c is free to mean "discard this draft"."""
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        for char in "hello":
            await pilot.press(char)
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app._prompt.text == ""
        assert app.is_running

        await pilot.press("ctrl+c")
        await pilot.pause()
        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
        assert "again to exit" in notices

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not app.is_running


async def test_ctrl_d_exits_only_from_an_empty_prompt(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        for char in "abc":
            await pilot.press(char)
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert app.is_running, "ctrl+d with text should delete, not quit"

        app._prompt.clear()
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert not app.is_running


async def test_typing_a_slash_opens_the_completion_popup(hx_home: Path, tmp_path: Path) -> None:
    """The placeholder has always promised this; now it happens."""
    from hx.tui.widgets.autocomplete import Autocomplete

    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        popup = app.query_one(Autocomplete)
        assert popup.display is False

        for key in ("slash", "c", "o"):
            await pilot.press(key)
        await pilot.pause()

        assert popup.display is True
        labels = [candidate.label for candidate in popup.completion.candidates]
        assert "/copy" in labels and "/compact" in labels

        await pilot.press("escape")
        await pilot.pause()
        assert popup.display is False


async def test_tab_accepts_the_highlighted_completion(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        for key in ("slash", "t", "h", "e"):
            await pilot.press(key)
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app._prompt.text == "/theme"


async def test_enter_runs_an_exact_slash_command_with_completion_open(
    hx_home: Path, tmp_path: Path
) -> None:
    """A complete command executes on the first Enter, even with autocomplete open."""
    from hx.tui.widgets.palette import ModelPicker

    app = build_app(tmp_path)
    app.models._models["openai/gpt-5"] = _gpt5()

    async with app.run_test() as pilot:
        old_session = app.loop.session.meta.session_id
        for key in ("slash", "c", "l", "e", "a", "r"):
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert app._prompt.text == ""
        assert app.loop.session.meta.session_id != old_session

        for key in ("slash", "m", "o", "d", "e", "l"):
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert isinstance(app.screen, ModelPicker)
        await pilot.press("escape")


async def test_ctrl_up_walks_back_through_messages(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        transcript = app._transcript
        transcript.add_user_message("first question")
        transcript.start_assistant_message()
        transcript.append_delta("first answer")
        transcript.add_user_message("second question")
        await pilot.pause()

        await pilot.press("ctrl+up")
        await pilot.pause()
        assert transcript.cursor is not None
        assert transcript.cursor.buffer == "second question"
        assert "cursored" in transcript.cursor.classes

        await pilot.press("ctrl+up")
        await pilot.pause()
        assert transcript.cursor.buffer == "first answer"

        await pilot.press("ctrl+down")
        await pilot.pause()
        assert transcript.cursor.buffer == "second question"


async def test_the_cursor_survives_the_bottom_of_the_list(hx_home: Path, tmp_path: Path) -> None:
    """Stepping past the end should stop, not wrap round to the top."""
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        app._transcript.add_user_message("only one")
        await pilot.pause()
        for _ in range(3):
            await pilot.press("ctrl+up")
        await pilot.pause()
        assert app._transcript.cursor.buffer == "only one"


async def test_ctrl_x_copies_the_message_under_the_cursor(hx_home: Path, tmp_path: Path) -> None:
    copied: list[str] = []
    app = build_app(tmp_path)

    async def fake_copy(text: str, **kwargs: Any) -> str:
        copied.append(text)
        return "pbcopy"

    async with app.run_test() as pilot:
        import hx.tui.clipboard as clipboard

        original = clipboard.copy_text
        clipboard.copy_text = fake_copy  # type: ignore[assignment]
        try:
            app._transcript.add_user_message("copy me")
            await pilot.pause()
            await pilot.press("ctrl+up")
            await pilot.press("ctrl+x")
            await pilot.pause()
        finally:
            clipboard.copy_text = original  # type: ignore[assignment]

    assert copied == ["copy me"]


async def test_copying_with_no_cursor_takes_the_last_answer(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        transcript = app._transcript
        transcript.add_user_message("question")
        transcript.start_assistant_message()
        transcript.append_delta("the answer")
        await pilot.pause()
        assert app.last_message_text() == "the answer"


async def test_expanding_with_a_cursor_touches_only_that_block(
    hx_home: Path, tmp_path: Path
) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        transcript = app._transcript
        transcript.add_tool_block("a", "Bash", {"command": "ls"})
        transcript.add_tool_block("b", "Bash", {"command": "pwd"})
        await pilot.pause()

        transcript.set_cursor(transcript._tools["a"])
        transcript.toggle_expanded()

        assert transcript._tools["a"].expanded is True
        assert transcript._tools["b"].expanded is False


async def test_expanding_with_no_cursor_still_means_all(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        transcript = app._transcript
        transcript.add_tool_block("a", "Bash", {"command": "ls"})
        transcript.add_tool_block("b", "Bash", {"command": "pwd"})
        await pilot.pause()

        transcript.toggle_expanded()

        assert transcript._tools["a"].expanded is True
        assert transcript._tools["b"].expanded is True


async def test_the_startup_header_expands_to_the_full_key_list(
    hx_home: Path, tmp_path: Path
) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        header = app._header
        compact = " ".join(str(row) for row in header.render().renderables)
        assert "shows every key" in compact

        await pilot.press("ctrl+o")
        await pilot.pause()
        expanded = " ".join(str(row) for row in header.render().renderables)
        assert "Cycle permission mode" in expanded


async def test_the_prompt_rules_track_focus(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._working.style_muted is False, "the prompt has focus at startup"


async def test_ctrl_c_interrupts_a_running_turn_instead_of_exiting(
    hx_home: Path, tmp_path: Path
) -> None:
    """Two presses of the universal stop chord must not tear down the session.

    The prompt is empty while a turn streams, which is exactly the state that
    arms the exit - so ctrl+c mid-answer used to quit HX instead of stopping it.
    """

    class SlowProvider:
        name = "slow"

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

        await app.action_clear()
        await app.action_clear()

        assert app.is_running, "ctrl+c during a turn must not exit"
        for _ in range(30):
            await pilot.pause(0.02)
            notices = " ".join(str(n.render()) for n in app.query_one(Transcript).query("Notice"))
            if "interrupted" in notices:
                break
        assert "interrupted" in notices


async def test_a_stale_ctrl_c_does_not_arm_the_exit(hx_home: Path, tmp_path: Path) -> None:
    """Arming has to expire with the activity that follows it, or a press from
    an hour ago silently counts as the first of two."""
    app = build_app(tmp_path)

    async with app.run_test() as pilot:
        await app.action_clear()
        assert app._clear_armed

        await app.submit("hello")
        await pilot.pause(0.1)
        assert not app._clear_armed, "submitting a turn disarms the pending exit"

        await app.action_clear()
        assert app.is_running
        assert app._clear_armed, "and re-arms with its own warning first"


async def test_title_command_shows_and_sets_the_session_name(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)

    async with app.run_test() as pilot:
        await app.submit("/title")
        await pilot.pause()
        await app.submit("/title parser rewrite")
        await pilot.pause()
        text = " ".join(str(n.render()) for n in app._transcript.query("Notice"))

    assert "Session title: app session" in text
    assert "Session title: parser rewrite" in text
    assert app.loop.session.meta.title == "parser rewrite"
    assert load_session(app.loop.session.meta.session_id).meta.title == "parser rewrite"


async def test_title_command_says_when_a_session_is_unnamed(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    app.loop.session.meta.title = None

    async with app.run_test() as pilot:
        await app.submit("/title")
        await pilot.pause()
        text = " ".join(str(n.render()) for n in app._transcript.query("Notice"))

    assert "not named yet" in text


async def test_prompt_command_shows_the_prompt_in_force(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    app.loop.context.system_prompt = "You are TESTBOT."

    async with app.run_test() as pilot:
        await app.submit("/prompt")
        await pilot.pause()
        text = " ".join(str(n.render()) for n in app._transcript.query("Notice"))

    assert "You are TESTBOT." in text
    assert "Source: built-in" in text
    # The built-in prompt is what a fresh resolve returns, so the in-use text
    # differs from it and the command must say the override applies next run.
    assert "applies next run" in text
