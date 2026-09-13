"""Session behaviour, driven through a fake terminal.

The behavioural half of what the Textual app's suite covered, carried over. It
asserts on what a terminal would show and on the session's own state, never on
a widget - which is what let it survive the frontend being replaced.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from hx.config import PermissionMode
from hx.core.messages import StopReason
from hx.core.usage import TurnUsage
from hx.providers.base import StreamDelta, StreamEnd
from tests.tui.support import MODEL, Driver, FakeProvider, build_session, gpt5, text_turn

pytestmark = pytest.mark.asyncio


# -- turns ------------------------------------------------------------------


async def test_a_turn_streams_into_the_transcript(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path, [text_turn("the model replied")])
    async with Driver(session) as driver:
        driver.type("a question\r")
        await driver.settle(rounds=40)
        assert "the model replied" in driver.transcript_text()


async def test_the_status_bar_reports_cost_and_cache_after_a_turn(
    hx_home: Path, tmp_path: Path
) -> None:
    usage = TurnUsage(
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=80,
        cache_write_tokens=10,
        cost_usd=0.25,
    )
    script = [[StreamDelta(text="hi"), StreamEnd(stop_reason=StopReason.END_TURN, usage=usage)]]
    session = build_session(tmp_path, script)
    async with Driver(session) as driver:
        driver.type("go\r")
        await driver.settle(rounds=40)
        status = session.view.dock.status
        assert status.input_tokens == 100
        assert status.cache_read == 80
        assert status.cost_usd == pytest.approx(0.25)


async def test_a_failed_tool_call_shows_its_reason(hx_home: Path, tmp_path: Path) -> None:
    from hx.core import events as ev

    session = build_session(tmp_path)
    async with Driver(session) as driver:
        session.bus.publish(
            ev.ToolCallStarted(tool_use_id="t1", name="Bash", input={"command": "false"})
        )
        await driver.settle()
        session.bus.publish(
            ev.ToolCallFinished(
                tool_use_id="t1",
                is_error=True,
                duration_ms=5.0,
                summary="exit 1",
                detail="command not found",
            )
        )
        await driver.settle()
        assert "command not found" in driver.transcript_text()


async def test_escape_cancels_a_streaming_turn(hx_home: Path, tmp_path: Path) -> None:
    async def forever() -> Any:
        while True:
            await asyncio.sleep(0.05)
            yield StreamDelta(text="…")

    session = build_session(tmp_path)
    session.loop.provider = FakeProvider([forever()])  # type: ignore[arg-type]
    async with Driver(session) as driver:
        driver.type("go\r")
        await driver.settle(rounds=20)
        driver.type("\x1b")
        await driver.settle(rounds=40)
        assert not session.is_busy


# -- the prompt and the keys ------------------------------------------------


async def test_enter_submits_and_ctrl_j_inserts_a_newline(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type("first")
        driver.type("\n")  # ctrl+j
        driver.type("second")
        await driver.settle()
        assert session.prompt.text == "first\nsecond"


async def test_ctrl_c_clears_a_draft_then_arms_then_exits(hx_home: Path, tmp_path: Path) -> None:
    """One keystroke should not end a session, so the exit is announced first."""
    session = build_session(tmp_path)
    driver = Driver(session)
    async with driver:
        driver.type("half a thought")
        await driver.settle()
        driver.type("\x03")
        await driver.settle()
        assert session.prompt.text == ""

        driver.type("\x03")
        await driver.settle()
        assert any("again to exit" in line for line in driver.display())


async def test_another_key_disarms_the_pending_exit(hx_home: Path, tmp_path: Path) -> None:
    """The user went back to work; a later ctrl+c means "clear", not "quit"."""
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type("\x03")
        await driver.settle()
        assert session._clear_armed
        driver.type("x")
        await driver.settle()
        assert not session._clear_armed


async def test_ctrl_d_exits_only_from_an_empty_prompt(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.05)
    try:
        session.runner.terminal.feed("typed")  # type: ignore[attr-defined]
        await asyncio.sleep(0.05)
        session.runner.terminal.feed("\x04")  # type: ignore[attr-defined]
        await asyncio.sleep(0.05)
        assert not task.done(), "ctrl+d exited with a draft in the prompt"
    finally:
        session.runner.stop()
        await asyncio.wait_for(task, timeout=5)


async def test_shift_tab_cycles_the_permission_mode(hx_home: Path, tmp_path: Path) -> None:
    from hx.permissions.engine import PermissionEngine

    session = build_session(tmp_path)
    session.loop.permissions = PermissionEngine(PermissionMode.DEFAULT, [], tmp_path)
    async with Driver(session) as driver:
        before = session.mode
        driver.type("\x1b[Z")
        await driver.settle()
        assert session.mode != before


# -- the reading cursor -----------------------------------------------------


async def test_ctrl_up_walks_back_through_messages(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path, [text_turn("an answer")])
    async with Driver(session) as driver:
        driver.type("a question\r")
        await driver.settle(rounds=40)

        driver.type("\x1b[1;5A")
        await driver.settle()
        assert session.view.transcript.cursor is not None
        assert "an answer" in session.view.transcript.cursored_text()

        driver.type("\x1b[1;5A")
        await driver.settle()
        assert "a question" in session.view.transcript.cursored_text()


async def test_the_cursor_stops_at_the_ends(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path, [text_turn("an answer")])
    async with Driver(session) as driver:
        driver.type("a question\r")
        await driver.settle(rounds=40)
        for _ in range(10):
            driver.type("\x1b[1;5A")
        await driver.settle()
        assert "a question" in session.view.transcript.cursored_text()


async def test_copying_with_no_cursor_takes_the_last_answer(hx_home: Path, tmp_path: Path) -> None:
    """What a user most often wants, without navigating first."""
    session = build_session(tmp_path, [text_turn("the answer to copy")])
    async with Driver(session) as driver:
        driver.type("a question\r")
        await driver.settle(rounds=40)
        assert "the answer to copy" in session.view.transcript.cursored_text()


async def test_copying_an_empty_transcript_says_so(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        await session.copy_cursored()
        await driver.settle()
        assert any("Nothing to copy" in line for line in driver.display())


async def test_expanding_with_a_cursor_touches_only_that_block(
    hx_home: Path, tmp_path: Path
) -> None:
    from hx.tui.renderers import ToolCall
    from hx.tui.views.blocks import ToolBlock

    session = build_session(tmp_path)
    async with Driver(session) as driver:
        first = ToolBlock(ToolCall("bash", {"command": "a"}, tmp_path, output="x", finished=True))
        second = ToolBlock(ToolCall("bash", {"command": "b"}, tmp_path, output="y", finished=True))
        session.view.transcript.append(first)
        session.view.transcript.append(second)
        session.view.transcript.cursor = second
        session.expand_cursored()
        await driver.settle()

        assert second.call.expanded
        assert not first.call.expanded


# -- shell passthrough ------------------------------------------------------


async def test_bang_runs_a_shell_command_without_a_model_turn(
    hx_home: Path, tmp_path: Path
) -> None:
    session = build_session(tmp_path, [text_turn("the model must not run")])
    session.loop.tools.register(_bash_tool(tmp_path))
    async with Driver(session) as driver:
        driver.type("!echo hello-from-shell\r")
        await driver.settle(rounds=80)

        shown = driver.transcript_text()
        assert "hello-from-shell" in shown
        assert "the model must not run" not in shown


async def test_bang_still_goes_through_the_permission_engine(hx_home: Path, tmp_path: Path) -> None:
    """A shortcut that skipped it would be a hole in the sandbox too."""
    from hx.permissions.engine import Decision, PermissionEngine, Rule

    session = build_session(tmp_path)
    session.loop.tools.register(_bash_tool(tmp_path))
    session.loop.permissions = PermissionEngine(
        PermissionMode.DEFAULT,
        [Rule(tool="Bash", specifier=None, decision=Decision.DENY, source="test")],
        tmp_path,
    )
    async with Driver(session) as driver:
        driver.type("!echo nope\r")
        await driver.settle(rounds=60)
        assert "Refused" in driver.transcript_text()


async def test_bang_without_a_shell_says_so(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type("!echo hi\r")
        await driver.settle(rounds=40)
        assert "No shell is attached" in driver.transcript_text()


def _bash_tool(cwd: Path) -> Any:
    from hx.tools.bash import BackgroundJobs, BashTool, PersistentShell

    return BashTool(PersistentShell(cwd), BackgroundJobs(cwd / "logs"))


# -- commands ---------------------------------------------------------------


async def test_every_registered_command_has_a_summary(hx_home: Path, tmp_path: Path) -> None:
    """A command in the palette that says nothing is worse than one that is
    not there yet."""
    from hx.tui.commands import build_default_commands

    for command in build_default_commands().all():
        assert command.summary.strip(), command.name


async def test_an_unknown_command_is_reported_not_sent_to_the_model(
    hx_home: Path, tmp_path: Path
) -> None:
    session = build_session(tmp_path, [text_turn("the model must not see this")])
    async with Driver(session) as driver:
        driver.type("/nosuchcommand\r")
        await driver.settle(rounds=40)
        shown = driver.transcript_text()
        assert "Unknown command" in shown
        assert "must not see this" not in shown


async def test_a_failing_command_does_not_kill_the_session(hx_home: Path, tmp_path: Path) -> None:
    from hx.tui.commands import Command

    session = build_session(tmp_path)
    async with Driver(session) as driver:

        async def explode(_ctx: Any, _args: str) -> None:
            raise RuntimeError("command blew up")

        session.commands.register(Command("boom", "explodes", explode))
        driver.type("/boom\r")
        await driver.settle(rounds=40)

        assert "command blew up" in driver.transcript_text()
        assert not session.runner._stopped.is_set()  # type: ignore[union-attr]


async def test_the_mode_command_changes_the_permission_mode(hx_home: Path, tmp_path: Path) -> None:
    from hx.permissions.engine import PermissionEngine

    session = build_session(tmp_path)
    session.loop.permissions = PermissionEngine(PermissionMode.DEFAULT, [], tmp_path)
    async with Driver(session) as driver:
        driver.type("/mode plan\r")
        await driver.settle(rounds=40)
        assert session.mode == "plan"


async def test_the_model_command_switches_the_model(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path)
    session.models._models = {
        "openai/gpt-5": gpt5(),
        MODEL: session.models.get_or_default(MODEL),
    }
    async with Driver(session) as driver:
        driver.type("/model openai/gpt-5\r")
        await driver.settle(rounds=60)
        assert session.loop.model == "openai/gpt-5"


async def test_the_model_command_reports_an_empty_catalogue(hx_home: Path, tmp_path: Path) -> None:
    from hx.providers.models import ModelRegistry

    empty = ModelRegistry()
    empty._models = {}
    session = build_session(tmp_path, models=empty)
    async with Driver(session) as driver:
        driver.type("/model nosuchmodel\r")
        await driver.settle(rounds=40)
        assert driver.transcript_text().strip()


# -- queueing and steering --------------------------------------------------


async def test_typing_during_a_turn_queues(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        session._turn = asyncio.create_task(asyncio.sleep(5))
        try:
            driver.type("a second thought\r")
            await driver.settle()
            assert session.queued == ["a second thought"]
        finally:
            session._turn.cancel()


async def test_clearing_the_queue_drops_everything_waiting(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        session._queued.extend(["one", "two"])
        session.clear_queue()
        await driver.settle()
        assert session.queued == []


async def test_alt_enter_with_nothing_running_just_submits(hx_home: Path, tmp_path: Path) -> None:
    """There is no tail to drain a queue, so the message would otherwise sit
    there being described as waiting on a turn that does not exist."""
    session = build_session(tmp_path, [text_turn("answered")])
    async with Driver(session) as driver:
        driver.type("a thought")
        driver.type("\x1b\r")  # alt+enter
        await driver.settle(rounds=40)
        assert "answered" in driver.transcript_text()


# -- theming ----------------------------------------------------------------


async def test_the_theme_command_repaints_everything(hx_home: Path, tmp_path: Path) -> None:
    """Every cached line holds the old colours, so the document is redrawn
    rather than diffed against them."""
    session = build_session(tmp_path, [text_turn("some text")])
    async with Driver(session) as driver:
        driver.type("a question\r")
        await driver.settle(rounds=40)
        before = driver.transcript_text()

        session.apply_theme("light")
        await driver.settle()
        assert driver.transcript_text() == before, "the text changed, not just the colour"
