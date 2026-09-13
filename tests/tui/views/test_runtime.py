"""A whole session on the new renderer, driven through a fake terminal.

The unit tests cover each block; this covers the wiring - that bus events reach
the right component, that a turn runs, and that what a terminal would display
is the conversation in the right order.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pyte
import pytest

from hx.config import load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.session import new_session
from hx.providers.models import ModelRegistry
from hx.term.terminal import FakeTerminal
from hx.tools.registry import ToolRegistry
from hx.tui import paint
from hx.tui.runtime import HXSession
from tests.tui.test_app import MODEL, FakeProvider, text_turn

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _pinned_colors() -> None:
    paint.set_color_mode("truecolor")


def build(tmp_path: Path, script: list[object] | None = None) -> HXSession:
    bus = EventBus()
    models = ModelRegistry()
    session = new_session(tmp_path, MODEL)
    session.set_title("test session")
    loop = AgentLoop(
        provider=FakeProvider(script if script is not None else [text_turn("an answer")]),
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
    return HXSession(loop, bus, load_settings(tmp_path), terminal=FakeTerminal(80, 24))


class Driver:
    """Runs a session, feeds it keys, and reads back what a terminal would show."""

    def __init__(self, session: HXSession) -> None:
        self.session = session
        self.terminal: FakeTerminal = session.runner.terminal  # type: ignore[assignment]
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Driver:
        self._task = asyncio.create_task(self.session.run())
        await self.settle()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.session.runner.stop()
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=5)

    async def settle(self, rounds: int = 12) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0.01)

    def type(self, text: str) -> None:
        self.terminal.feed(text)

    def display(self) -> list[str]:
        vt = pyte.Screen(80, 24)
        pyte.Stream(vt).feed(self.terminal.output)
        return [line.rstrip() for line in vt.display if line.strip()]


async def test_a_session_opens_with_a_header_and_a_prompt(hx_home: Path, tmp_path: Path) -> None:
    async with Driver(build(tmp_path)) as driver:
        shown = driver.display()
        assert any("hx v" in line for line in shown)
        assert any("Ask HX…" in line for line in shown)


async def test_typing_reaches_the_prompt(hx_home: Path, tmp_path: Path) -> None:
    async with Driver(build(tmp_path)) as driver:
        driver.type("a question")
        await driver.settle()
        assert driver.session.view.dock.prompt.value == "a question"
        assert any("a question" in line for line in driver.display())


async def test_submitting_runs_a_turn_and_shows_both_sides(hx_home: Path, tmp_path: Path) -> None:
    async with Driver(build(tmp_path, [text_turn("the model replied")])) as driver:
        driver.type("a question\r")
        await driver.settle(rounds=60)

        shown = driver.display()
        question = next(i for i, line in enumerate(shown) if "a question" in line)
        answer = next(i for i, line in enumerate(shown) if "the model replied" in line)
        assert question < answer, "the reply drew above the question"


async def test_the_prompt_is_cleared_after_submitting(hx_home: Path, tmp_path: Path) -> None:
    async with Driver(build(tmp_path)) as driver:
        driver.type("a question\r")
        await driver.settle(rounds=40)
        assert driver.session.view.dock.prompt.value == ""


async def test_ctrl_c_clears_a_draft_before_it_exits(hx_home: Path, tmp_path: Path) -> None:
    """Two meanings for one key, and the destructive one has to be second."""
    driver = Driver(build(tmp_path))
    async with driver:
        driver.type("half a thought")
        await driver.settle()
        driver.type("\x03")
        await driver.settle()
        assert driver.session.view.dock.prompt.value == ""


async def test_an_error_is_surfaced_as_a_notice(hx_home: Path, tmp_path: Path) -> None:
    from hx.core import events as ev

    session = build(tmp_path)
    async with Driver(session) as driver:
        session.bus.publish(ev.ErrorRaised(message="the provider returned 429"))
        await driver.settle()
        assert any("429" in line for line in driver.display())


async def test_usage_events_reach_the_status_bar(hx_home: Path, tmp_path: Path) -> None:
    from hx.core import events as ev

    session = build(tmp_path)
    async with Driver(session) as driver:
        session.bus.publish(
            ev.UsageUpdated(
                input_tokens=57_000,
                output_tokens=3_500,
                cache_read_tokens=244_000,
                cache_write_tokens=12_000,
                context_tokens=24_000,
                context_window=200_000,
                cost_usd=0.16,
            )
        )
        await driver.settle()
        assert session.view.dock.status.input_tokens == 57_000
        assert any("↑57k" in line for line in driver.display())


async def test_leaving_hands_the_terminal_back(hx_home: Path, tmp_path: Path) -> None:
    driver = Driver(build(tmp_path))
    async with driver:
        pass
    assert driver.terminal.restored


TODOS = [
    {"content": "Port the renderers", "status": "completed", "active_form": "Porting them"},
    {"content": "Draw the transcript", "status": "in_progress", "active_form": "Drawing it"},
]


async def _write_todos(driver: Driver, *, is_error: bool = False) -> None:
    """The event sequence a real TodoWrite produces, in order."""
    from hx.core import events as ev

    bus = driver.session.bus
    bus.publish(ev.ToolCallStarted(tool_use_id="t1", name="TodoWrite", input={"todos": TODOS}))
    if not is_error:
        bus.publish(ev.TodosUpdated(todos=TODOS))
    bus.publish(
        ev.ToolCallFinished(
            tool_use_id="t1",
            is_error=is_error,
            duration_ms=1.0,
            summary="error" if is_error else "Todo list updated: 1/2 complete.",
            detail="unknown todo status 'doing'" if is_error else "",
        )
    )
    await driver.settle()


async def test_a_plan_is_drawn_once_not_once_per_block(hx_home: Path, tmp_path: Path) -> None:
    """The call renders the list and so does the block that follows it.

    In the Textual app those were two regions - an inline call and a sidebar -
    so nobody saw them together. Here they would land in the same column.
    """
    async with Driver(build(tmp_path)) as driver:
        await _write_todos(driver)

        shown = driver.display()
        assert sum("Port the renderers" in line for line in shown) == 1
        assert any("Todos" in line for line in shown), "the plan was dropped, not deduplicated"


async def test_a_failed_todo_write_still_shows_its_block(hx_home: Path, tmp_path: Path) -> None:
    """A failure publishes no TodosUpdated, so the held block is all there is."""
    async with Driver(build(tmp_path)) as driver:
        await _write_todos(driver, is_error=True)

        shown = driver.display()
        assert any("unknown todo status" in line for line in shown)


async def test_a_tool_call_that_needs_approval_blocks_and_is_answered(
    hx_home: Path, tmp_path: Path
) -> None:
    """The whole path: the loop asks, the block appears in the transcript, one
    key answers it, and the record is left behind."""
    from hx.permissions.engine import Decision, PermissionEngine, Rule

    session = build(tmp_path)
    session.loop.permissions = PermissionEngine(
        mode=session.settings.permissions.mode,
        rules=[Rule(tool="Bash", specifier=None, decision=Decision.ASK, source="test")],
        cwd=tmp_path,
    )

    async with Driver(session) as driver:
        request = _request(tmp_path)
        answer = asyncio.create_task(session.ask_permission(request))
        await driver.settle()

        shown = driver.display()
        assert any("Permission needed" in line for line in shown)
        assert any("$ rm -rf build/" in line for line in shown)
        assert any("allow for this session" in line for line in shown)

        driver.type("s")
        await driver.settle()

        result = await asyncio.wait_for(answer, timeout=2)
        assert result.allowed is True
        assert any("allowed for this session" in line for line in driver.display())


async def test_typing_while_an_approval_is_open_does_not_reach_the_prompt(
    hx_home: Path, tmp_path: Path
) -> None:
    """ "y" is an answer, not a character. The prompt must not eat it."""
    session = build(tmp_path)
    async with Driver(session) as driver:
        answer = asyncio.create_task(session.ask_permission(_request(tmp_path)))
        await driver.settle()

        driver.type("y")
        await driver.settle()

        assert session.view.dock.prompt.value == ""
        assert (await asyncio.wait_for(answer, timeout=2)).allowed is True


async def test_an_interrupted_approval_claims_no_decision(hx_home: Path, tmp_path: Path) -> None:
    session = build(tmp_path)
    async with Driver(session) as driver:
        answer = asyncio.create_task(session.ask_permission(_request(tmp_path)))
        await driver.settle()
        answer.cancel()
        await driver.settle()

        shown = driver.display()
        assert any("not answered" in line for line in shown)
        assert not any("allowed" in line for line in shown)


def _request(cwd: Path) -> object:
    from hx.permissions.engine import PermissionRequest

    return PermissionRequest(
        tool_name="Bash",
        specifier="rm -rf build/",
        params={"command": "rm -rf build/"},
        mutating=True,
        description="run a shell command",
        detail="rm -rf build/",
        detail_kind="command",
    )
