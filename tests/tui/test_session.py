"""Session behaviour, driven through a fake terminal.

The behavioural half of what the Textual app's suite covered, carried over. It
asserts on what a terminal would show and on the session's own state, never on
a widget - which is what let it survive the frontend being replaced.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any

import pytest

from hx.config import PermissionMode
from hx.core.messages import StopReason
from hx.core.usage import TurnUsage
from hx.providers.base import StreamDelta, StreamEnd
from hx.providers.fake import Pause
from tests.tui.support import (
    COLUMNS,
    MODEL,
    Driver,
    build_session,
    gpt5,
    text_turn,
)

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
    # Parked mid-stream rather than looping forever: a stream that yields in a
    # loop is never actually caught in the act - the script is consumed
    # synchronously - so the turn was over before escape arrived and the test
    # passed without cancelling anything.
    session = build_session(tmp_path, [[StreamDelta(text="…"), Pause()]])
    async with Driver(session) as driver:
        driver.type("go\r")
        await driver.settle(rounds=20)
        assert session.is_busy, "the turn is not running, so there is nothing to cancel"

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


# -- untrusted text ---------------------------------------------------------

#: Sequences a terminal acts on. If one of these reaches the tty, whatever
#: produced it is driving the user's terminal.
HOSTILE = (
    "\x1b]52;c;cGF5bG9hZA==\x07"  # write the clipboard
    "\x1b]0;PWNED\x07"  # rename the window
    "\x1b]8;;http://evil\x07link\x1b]8;;\x07"  # link somewhere else
    "\x1b[2J\x1b[H"  # erase the screen, home the cursor
    "\x1b_hx:c\x07"  # the renderer's own cursor marker
    "\x9b2J"  # the same attack in the C1 range
)

MARKERS = ("\x1b]52;", "\x1b]0;PWNED", "\x1b]8;;http://evil", "\x1b[2J", "\x1b_hx:c", "\x9b")


def _assert_inert(driver: Driver, where: str) -> None:
    for marker in MARKERS:
        assert marker not in driver.terminal.output, f"{where} reached the terminal: {marker!r}"


async def test_a_models_reply_cannot_drive_the_terminal(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path, [text_turn(f"here you go {HOSTILE} done")])
    async with Driver(session) as driver:
        driver.type("a question\r")
        await driver.settle(rounds=40)
        _assert_inert(driver, "a model's reply")
        assert "here you go" in driver.transcript_text()


async def test_tool_output_cannot_drive_the_terminal(hx_home: Path, tmp_path: Path) -> None:
    """A file read by a tool is the most ordinary way hostile bytes arrive."""
    from hx.tui.renderers import ToolCall
    from hx.tui.views.blocks import ToolBlock

    session = build_session(tmp_path)
    async with Driver(session) as driver:
        block = ToolBlock(
            ToolCall(name="Bash", params={"command": f"cat {HOSTILE} log"}, cwd=tmp_path)
        )
        session.view.transcript.append(block)
        block.update(
            finished=True,
            is_error=False,
            summary=f"read {HOSTILE}",
            output=f"line one\n{HOSTILE}\nline three",
            expanded=True,
        )
        session.runner.request_immediate_render()
        await driver.settle()
        _assert_inert(driver, "tool output")
        assert "line three" in driver.transcript_text()


async def test_streamed_tool_output_cannot_drive_the_terminal(
    hx_home: Path, tmp_path: Path
) -> None:
    """Each chunk is sanitized on its own, so a sequence split across two of
    them must not reassemble."""
    from hx.tui.renderers import ToolCall
    from hx.tui.views.blocks import ToolBlock

    session = build_session(tmp_path)
    async with Driver(session) as driver:
        block = ToolBlock(ToolCall(name="Bash", params={"command": "run"}, cwd=tmp_path))
        session.view.transcript.append(block)
        block.update(expanded=True)
        session._append_output(block, "safe \x1b]52;c;cGF5")
        session._append_output(block, "bG9hZA==\x07 tail")
        session.runner.request_immediate_render()
        await driver.settle()
        _assert_inert(driver, "a split payload")


async def test_a_paste_into_the_prompt_cannot_drive_the_terminal(
    hx_home: Path, tmp_path: Path
) -> None:
    """The draft is drawn as it is typed, so this has to come off before the
    user presses enter rather than on submit."""
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type(f"\x1b[200~pasted {HOSTILE} log\x1b[201~")
        await driver.settle()
        _assert_inert(driver, "a paste")
        assert "pasted" in session.prompt.value

        driver.type("\r")
        await driver.settle(rounds=40)
        _assert_inert(driver, "a submitted paste")


async def test_a_notice_cannot_drive_the_terminal(hx_home: Path, tmp_path: Path) -> None:
    """An error message is very often a tool's stderr wearing a sentence."""
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        session.notice(f"it failed: {HOSTILE}", "error")
        session.runner.request_immediate_render()
        await driver.settle()
        _assert_inert(driver, "a notice")
        assert "it failed" in driver.transcript_text()


async def test_a_plan_cannot_drive_the_terminal(hx_home: Path, tmp_path: Path) -> None:
    """A todo is the model's own words and reaches the screen without a block."""
    from hx.tui.views.blocks import TodoBlock

    session = build_session(tmp_path)
    async with Driver(session) as driver:
        session.view.transcript.append(
            TodoBlock([{"content": f"do the thing {HOSTILE}", "status": "pending"}])
        )
        session.runner.request_immediate_render()
        await driver.settle()
        _assert_inert(driver, "a plan")
        assert "do the thing" in driver.transcript_text()


async def test_an_approval_cannot_drive_the_terminal(hx_home: Path, tmp_path: Path) -> None:
    """The one block whose whole job is to show what is about to happen."""
    from hx.permissions.engine import PermissionRequest

    session = build_session(tmp_path)
    async with Driver(session) as driver:
        command = f"rm -rf / {HOSTILE} --no-preserve-root"
        asked = asyncio.create_task(
            session.ask_permission(
                PermissionRequest(
                    tool_name="Bash",
                    specifier=command,
                    params={"command": command},
                    mutating=True,
                    description=f"Bash({command})",
                    detail=command,
                    detail_kind="command",
                )
            )
        )
        await driver.settle()
        _assert_inert(driver, "an approval")
        assert "no-preserve-root" in driver.transcript_text()

        driver.type("n")
        await driver.settle()
        _assert_inert(driver, "an answered approval")
        await asked


# -- approvals --------------------------------------------------------------


def _ask(tool: str, command: str) -> Any:
    from hx.permissions.engine import PermissionRequest

    return PermissionRequest(
        tool_name=tool,
        specifier=command,
        params={"command": command},
        mutating=False,
        description=f"{tool}({command})",
        detail=command,
        detail_kind="command",
    )


async def test_two_concurrent_approvals_are_answered_oldest_first(
    hx_home: Path, tmp_path: Path
) -> None:
    """Concurrent subagents can each stop at one, and only one has the keyboard.

    With a single slot the second displaced the first, and the first was left
    on screen offering keys that reached nothing while whoever asked waited on
    it forever.
    """
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        first = asyncio.create_task(session.ask_permission(_ask("Bash", "ls")))
        await driver.settle()
        second = asyncio.create_task(session.ask_permission(_ask("Bash", "pwd")))
        await driver.settle()

        driver.type("y")
        await driver.settle()
        assert first.done(), "the older approval did not get the keyboard"
        assert not second.done()

        driver.type("n")
        await driver.settle()
        assert second.done(), "the second approval was left unanswerable"

        assert (await first).allowed
        assert not (await second).allowed


async def test_an_approval_waiting_its_turn_says_so(hx_home: Path, tmp_path: Path) -> None:
    """Two identical live dialogs would be two sets of keys that look live."""
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        first = asyncio.create_task(session.ask_permission(_ask("Bash", "ls")))
        await driver.settle()
        second = asyncio.create_task(session.ask_permission(_ask("Bash", "pwd")))
        await driver.settle()

        text = driver.transcript_text()
        assert "answer the approval above first" in text
        assert text.count("allow once") == 1, "both blocks offered the answer keys"

        driver.type("y")
        await driver.settle()
        assert "answer the approval above first" not in driver.transcript_text()

        driver.type("y")
        await driver.settle()
        for task in (first, second):
            await task


async def test_the_working_indicator_waits_for_the_last_approval(
    hx_home: Path, tmp_path: Path
) -> None:
    """With one still open the turn is still blocked on a person."""
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        session.view.dock.working.start()
        first = asyncio.create_task(session.ask_permission(_ask("Bash", "ls")))
        await driver.settle()
        second = asyncio.create_task(session.ask_permission(_ask("Bash", "pwd")))
        await driver.settle()
        assert not session.view.dock.working.running

        driver.type("y")
        await driver.settle()
        assert not session.view.dock.working.running, "the indicator restarted too early"

        driver.type("y")
        await driver.settle()
        assert session.view.dock.working.running
        for task in (first, second):
            await task


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


async def test_a_queued_message_is_sent_when_the_turn_ends(hx_home: Path, tmp_path: Path) -> None:
    """Held rather than dropped means it eventually goes out.

    A queue nothing drains is a queue of messages that look sent and never
    were: the user pressed enter, the transcript shows what they typed, and
    the model never hears about it.
    """
    session = build_session(tmp_path, [text_turn("first answer"), text_turn("second answer")])

    release = asyncio.Event()
    run_turn = session.loop.run

    async def held(text: str) -> None:
        if text == "one":
            await release.wait()
        await run_turn(text)

    session.loop.run = held  # type: ignore[method-assign]

    async with Driver(session) as driver:
        driver.type("one\r")
        await driver.settle()
        driver.type("two\r")
        await driver.settle()
        assert session.queued == ["two"]

        release.set()
        await driver.settle(rounds=60)
        assert session.queued == []
        assert "second answer" in driver.transcript_text()


async def test_an_interrupt_keeps_the_queue_rather_than_draining_it(
    hx_home: Path, tmp_path: Path
) -> None:
    """The user said stop. Starting the next turn is the opposite of that."""
    session = build_session(tmp_path)

    async def never(text: str) -> None:
        await asyncio.Event().wait()

    session.loop.run = never  # type: ignore[method-assign]

    async with Driver(session) as driver:
        driver.type("one\r")
        await driver.settle()
        driver.type("two\r")
        await driver.settle()
        assert session.queued == ["two"]

        driver.type("\x1b")  # escape
        await driver.settle(rounds=30)
        assert session.queued == ["two"], "the interrupt drained the queue"
        assert "still queued" in driver.transcript_text()


async def test_enter_while_busy_steers_when_the_setting_says_so(
    hx_home: Path, tmp_path: Path
) -> None:
    """``tui.enterWhileBusy: steer`` reached the placeholder and nothing else,
    so the prompt promised enter would steer while enter went on queueing."""
    from dataclasses import replace

    from hx.config import EnterWhileBusy

    session = build_session(tmp_path)
    session.settings = replace(
        session.settings,
        tui=replace(session.settings.tui, enter_while_busy=EnterWhileBusy.STEER),
    )

    steered: list[str] = []
    session.loop.steer = steered.append  # type: ignore[method-assign]

    async def never(text: str) -> None:
        await asyncio.Event().wait()

    session.loop.run = never  # type: ignore[method-assign]

    async with Driver(session) as driver:
        driver.type("one\r")
        await driver.settle()
        driver.type("two\r")
        await driver.settle()

        assert steered == ["two"], "enter queued instead of steering"
        assert session.queued == []


async def test_steering_an_empty_prompt_promotes_the_queue(hx_home: Path, tmp_path: Path) -> None:
    """The only reading of "steer" that means anything with nothing typed.
    Steering the empty string put a blank message into the turn."""
    session = build_session(tmp_path)

    steered: list[str] = []
    session.loop.steer = steered.append  # type: ignore[method-assign]

    async def never(text: str) -> None:
        await asyncio.Event().wait()

    session.loop.run = never  # type: ignore[method-assign]

    async with Driver(session) as driver:
        driver.type("one\r")
        await driver.settle()
        driver.type("two\r")
        await driver.settle()
        assert session.queued == ["two"]

        driver.type("\x1b\r")  # alt+enter on an empty prompt
        await driver.settle()

        assert steered == ["two"], "an empty steer did not promote the queue"
        assert session.queued == []


async def test_the_queue_commands_match_what_slash_queue_calls(
    hx_home: Path, tmp_path: Path
) -> None:
    """``/queue clear`` reports the count, and ``/queue steer n`` takes one."""
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        session._queued.extend(["one", "two"])
        session.steer_queued(1)
        await driver.settle()
        assert session.queued == ["one"]
        assert session.clear_queue() == 1
        assert session.queued == []


async def test_a_slash_command_does_not_displace_the_running_turn(
    hx_home: Path, tmp_path: Path
) -> None:
    """They used to share one handle, so escape cancelled the wrong one."""
    session = build_session(tmp_path)

    async def never(text: str) -> None:
        await asyncio.Event().wait()

    session.loop.run = never  # type: ignore[method-assign]

    async with Driver(session) as driver:
        driver.type("a question\r")
        await driver.settle()
        turn = session._turn

        driver.type("/help\r")
        await driver.settle(rounds=20)
        assert session._turn is turn
        assert session.is_busy


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


# -- the two screens ---------------------------------------------------------


async def test_fullscreen_pins_the_prompt_to_the_bottom_row(hx_home: Path, tmp_path: Path) -> None:
    """The point of the mode: the prompt is where it was, not under whatever
    the conversation did."""
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type("/fullscreen\r")
        await driver.settle(rounds=40)
        assert session.fullscreen is True
        assert driver.terminal.alt_screen is True

        screen = driver.screen()
        assert screen[-1].strip(), "the bottom row is empty"
        assert "Ask HX" in "\n".join(screen[-6:]), "the prompt is not at the bottom"


async def test_fullscreen_off_returns_the_terminal(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type("/fullscreen on\r")
        await driver.settle(rounds=40)
        driver.type("/fullscreen off\r")
        await driver.settle(rounds=40)
        assert session.fullscreen is False
        assert driver.terminal.alt_screen is False


async def test_fullscreen_rejects_anything_that_is_not_on_or_off(
    hx_home: Path, tmp_path: Path
) -> None:
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type("/fullscreen sideways\r")
        await driver.settle(rounds=40)
        assert session.fullscreen is False
        assert "Usage: /fullscreen" in driver.transcript_text()


async def test_page_keys_scroll_only_in_fullscreen(hx_home: Path, tmp_path: Path) -> None:
    """On the normal screen the terminal is the scroller, and taking the key
    would replace something that already works."""
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        for index in range(60):
            session.notice(f"line {index}")
        await driver.settle()

        driver.type("\x1b[5~")  # pageup
        await driver.settle()
        assert session.runner.screen._scroll == 0

        driver.type("/fullscreen\r")
        await driver.settle(rounds=40)
        driver.type("\x1b[5~")
        await driver.settle()
        assert session.runner.screen._scroll > 0, "pageup did not scroll the viewport"

        driver.type("\x1b[1;5F")  # ctrl+end
        await driver.settle()
        assert session.runner.screen._scroll == 0, "ctrl+end did not return to the newest line"


async def test_ctrl_z_suspends(hx_home: Path, tmp_path: Path) -> None:
    """Raw mode is exactly what stops the terminal doing this for us, so the
    key has to reach the terminal by hand."""
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type("\x1a")
        await driver.settle()
        assert driver.terminal.suspends == 1


async def test_ctrl_home_walks_to_the_first_message_on_the_normal_screen(
    hx_home: Path, tmp_path: Path
) -> None:
    session = build_session(tmp_path, [text_turn("one"), text_turn("two")])
    async with Driver(session) as driver:
        driver.type("first\r")
        await driver.settle(rounds=40)
        driver.type("second\r")
        await driver.settle(rounds=40)

        driver.type("\x1b[1;5H")  # ctrl+home
        await driver.settle()
        cursor = session.view.transcript.cursor
        assert cursor is session.view.transcript.navigable()[0]

        driver.type("\x1b[1;5F")  # ctrl+end
        await driver.settle()
        assert session.view.transcript.cursor is session.view.transcript.navigable()[-1]


# -- clearing ----------------------------------------------------------------


async def test_clear_wipes_the_screen_and_the_scrollback(hx_home: Path, tmp_path: Path) -> None:
    """Emptying the transcript and leaving the old session on screen above it
    is not what anybody means by clear."""
    session = build_session(tmp_path, [text_turn("the old conversation")])
    async with Driver(session) as driver:
        driver.type("something\r")
        await driver.settle(rounds=40)
        driver.terminal.clear_output()

        driver.type("/clear\r")
        await driver.settle(rounds=40)

        assert "\x1b[3J" in driver.terminal.output, "the scrollback was left behind"
        assert "the old conversation" not in driver.screen_text()


async def test_clear_during_a_turn_is_refused(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path, [[StreamDelta(text="working"), Pause()]])
    async with Driver(session) as driver:
        driver.type("go\r")
        await driver.settle(rounds=20)
        assert session.is_busy

        driver.type("/clear\r")
        await driver.settle(rounds=20)
        assert "Interrupt the running turn" in driver.transcript_text()

        driver.type("\x1b")
        await driver.settle(rounds=20)


# -- overlays ----------------------------------------------------------------


async def test_a_picker_uses_the_height_of_the_terminal(hx_home: Path, tmp_path: Path) -> None:
    """Eight rows of a two-hundred entry catalogue, with two thirds of a tall
    window empty underneath, was the old behaviour on every terminal."""
    from hx.tui.views.pickers import Picker

    session = build_session(tmp_path)

    class Many(Picker):
        title = "Many"

        def rows(self, query: str) -> list[tuple[str, list[str]]]:
            return [(f"value-{index}", [f"value-{index}"]) for index in range(200)]

    async with Driver(session) as driver:
        # Taller than the default, because at 24 rows a 7-row dock and the
        # frame leave exactly the eight this is here to stop being a constant.
        rows = 40
        driver.terminal.resize(COLUMNS, rows)
        picker = Many()
        session.show(picker)
        await driver.settle()

        shown = len(picker._window())
        assert shown > 8, "the picker ignored the room it had"
        dock = len(session.view.dock.render(COLUMNS))
        gap = session.view.OVERLAY_GAP
        assert shown + Picker.CHROME - 2 + 1 + dock + gap <= rows, (
            "the picker overflowed the screen"
        )


async def test_a_docked_overlay_does_not_draw_a_rule_against_the_dock(
    hx_home: Path, tmp_path: Path
) -> None:
    """The dialog's closing rule, a blank line and the prompt's rule made three
    lines of frame for one edge - which reads as a rendering fault."""
    from hx.tui.views.pickers import CommandPalette

    session = build_session(tmp_path)
    async with Driver(session) as driver:
        session.show(CommandPalette(list(session.commands.all())))
        await driver.settle()

        lines = [line.rstrip() for line in driver.screen()]
        rules = [index for index, line in enumerate(lines) if line and set(line) <= {"─", " "}]
        for first, second in itertools.pairwise(rules):
            if second - first == 2:
                assert lines[first + 1].strip(), (
                    f"rule, blank, rule at rows {first}-{second}: {lines[first : second + 1]}"
                )


async def test_an_approval_against_the_dock_does_not_draw_a_rule_either(
    hx_home: Path, tmp_path: Path
) -> None:
    """An approval is the block most often sitting directly above the dock, and
    it is framed like a dialog - so it had the three-line seam too."""
    from hx.permissions.engine import PermissionRequest

    session = build_session(tmp_path)
    async with Driver(session) as driver:
        asked = asyncio.create_task(
            session.ask_permission(
                PermissionRequest(
                    tool_name="Bash",
                    specifier="ls -la",
                    params={"command": "ls -la"},
                    mutating=False,
                    description="Bash(ls -la)",
                    detail="ls -la",
                    detail_kind="command",
                )
            )
        )
        await driver.settle()

        lines = [line.rstrip() for line in driver.screen()]
        rules = [index for index, line in enumerate(lines) if line and set(line) <= {"─", " "}]
        for first, second in itertools.pairwise(rules):
            if second - first == 2:
                assert lines[first + 1].strip(), (
                    f"rule, blank, rule at rows {first}-{second}: {lines[first : second + 1]}"
                )

        driver.type("n")
        await driver.settle()
        await asked


async def test_a_block_stops_being_docked_once_something_lands_under_it(
    hx_home: Path, tmp_path: Path
) -> None:
    """Which block the dock closes changes as the conversation grows, so it is
    decided per frame rather than when the block was made."""
    from hx.tui.views.blocks import UserMessage
    from hx.tui.views.pickers import CommandPalette

    session = build_session(tmp_path)
    async with Driver(session) as driver:
        picker = CommandPalette(list(session.commands.all()))
        session.show(picker)
        await driver.settle()
        assert picker.docked, "the picker never took the dock's rule"

        session.dismiss_modal()
        session.view.transcript.append(UserMessage("something after it"))
        await driver.settle()
        assert not picker.docked, "the picker is still closing on the dock's rule"


async def test_the_prompt_can_undo_and_redo(hx_home: Path, tmp_path: Path) -> None:
    """The editor had the operation and no key bound to it, while the README
    said ctrl+z undid - which by then was the suspend key."""
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type("hello")
        await driver.settle()
        driver.type(" world")
        await driver.settle()
        assert session.prompt.value == "hello world"

        driver.type("\x1f")  # ctrl+_
        await driver.settle()
        assert session.prompt.value != "hello world", "undo did nothing"

        driver.type("\x1b[122;6u")  # ctrl+shift+z, redo
        await driver.settle()
        assert session.prompt.value == "hello world"


async def test_the_completion_list_never_moves_the_prompt(hx_home: Path, tmp_path: Path) -> None:
    """The dock is on the bottom row, so a list drawn under the prompt would
    shove the prompt, the hints and the status bar up by one row per match as
    the query narrows - the whole dock twitching on every keystroke."""
    session = build_session(tmp_path)

    def dock_rows(driver: Driver, typed: str) -> dict[str, int]:
        rows = driver.screen()
        return {
            name: next(index for index, line in enumerate(rows) if probe(line))
            for name, probe in (
                ("prompt", lambda line: line.strip() == typed),
                ("hints", lambda line: "ctrl+p palette" in line),
                ("status", lambda line: "(auto)" in line),
            )
        }

    async with Driver(session) as driver:
        driver.type("/c")
        await driver.settle()
        before = dock_rows(driver, "/c")
        listed = [line for line in driver.screen() if "Start a fresh session" in line]
        assert listed, "the command list never opened"

        driver.type("lea")  # /clear, one match left
        await driver.settle()

        assert dock_rows(driver, "/clea") == before, "the dock moved as the list shrank"


async def test_the_completion_list_sits_above_the_prompt(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type("/clea")
        await driver.settle()

        rows = driver.screen()
        listed = next(index for index, line in enumerate(rows) if "Start a fresh session" in line)
        typed = next(index for index, line in enumerate(rows) if line.strip() == "/clea")
        assert listed < typed, "the list is under the prompt, pushing the dock around"


async def test_exiting_takes_the_whole_interface_off_the_screen(
    hx_home: Path, tmp_path: Path
) -> None:
    """The terminal is left as HX found it: the shell, and nothing else."""
    session = build_session(tmp_path, [text_turn("the model replied")])
    driver = Driver(session)
    task = asyncio.create_task(session.run())
    await driver.settle()
    driver.type("hello\r")
    await driver.settle(40)
    driver.type("/exit\r")
    await asyncio.wait_for(task, timeout=5)

    assert driver.display() == [], f"HX left this behind: {driver.display()}"
