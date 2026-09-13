"""Render scheduling and key dispatch."""

from __future__ import annotations

import asyncio

import pytest

from hx.term.component import Container
from hx.term.keydecode import Key
from hx.term.loop import ESCAPE_TIMEOUT, MIN_RENDER_INTERVAL, TuiRunner
from hx.term.primitives import Text
from hx.term.terminal import FakeTerminal

pytestmark = pytest.mark.asyncio


class Runner:
    """A started runner with a fake terminal, torn down on exit."""

    def __init__(self) -> None:
        self.terminal = FakeTerminal(40, 12)
        self.root = Container()
        self.keys: list[Key] = []
        self.runner = TuiRunner(self.root, self.terminal, on_key=self.keys.append)
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Runner:
        self._task = asyncio.create_task(self.runner.run())
        await asyncio.sleep(0)  # let run() reach the wait
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.runner.stop()
        if self._task is not None:
            await self._task

    def feed(self, data: str) -> None:
        self.terminal.feed(data)

    @property
    def output(self) -> str:
        return self.terminal.output


async def test_the_first_frame_is_drawn_on_start() -> None:
    async with Runner() as r:
        r.root.add(Text("hello"))
        r.runner.request_immediate_render()
        assert "hello" in r.output


async def test_a_keystroke_is_dispatched_and_drawn_at_once() -> None:
    """A character that appears a frame later reads as lag."""
    async with Runner() as r:
        label = r.root.add(Text(""))
        r.feed("a")
        assert [key.name for key in r.keys] == ["text"]
        assert r.keys[0].data == "a"

        label.set_text("a")
        r.runner.request_immediate_render()
        assert "a" in r.output


async def test_repeated_requests_collapse_into_one_frame() -> None:
    """A streaming model invalidates the document hundreds of times a second."""
    async with Runner() as r:
        label = r.root.add(Text("start"))
        r.runner.request_immediate_render()
        r.terminal.clear_output()

        for step in range(50):
            label.set_text(f"step {step}")
            r.runner.request_render()

        await asyncio.sleep(MIN_RENDER_INTERVAL * 2)
        assert "step 49" in r.output
        assert "step 0" not in r.output, "drew an intermediate state"


async def test_a_lone_escape_is_reported_once_nothing_follows_it() -> None:
    """The terminal sends the same byte for Escape and for the start of every
    arrow key, so it is held - but it still has to arrive."""
    async with Runner() as r:
        r.feed("\x1b")
        assert r.keys == []
        await asyncio.sleep(ESCAPE_TIMEOUT * 3)
        assert [key.name for key in r.keys] == ["escape"]


async def test_an_arrow_arriving_in_two_reads_is_not_seen_as_escape() -> None:
    async with Runner() as r:
        r.feed("\x1b")
        r.feed("[A")
        await asyncio.sleep(ESCAPE_TIMEOUT * 3)
        assert [key.name for key in r.keys] == ["up"]


async def test_a_resize_redraws_immediately() -> None:
    async with Runner() as r:
        r.root.add(Text("the quick brown fox jumps over the lazy dog"))
        r.runner.request_immediate_render()
        r.terminal.clear_output()

        r.terminal.resize(20, 12)
        assert r.output != "", "a resize left the screen stale"


async def test_keys_reach_the_component_tree_when_there_is_no_handler() -> None:
    seen: list[tuple[str, str]] = []

    class Listening(Text):
        def handle_input(self, key: str, data: str) -> bool:
            seen.append((key, data))
            return True

    terminal = FakeTerminal(40, 12)
    root = Container(Listening("x"))
    runner = TuiRunner(root, terminal)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        terminal.feed("\x03")
        assert seen == [("ctrl+c", "\x03")]
    finally:
        runner.stop()
        await task


async def test_stopping_hands_the_terminal_back() -> None:
    r = Runner()
    async with r:
        r.root.add(Text("bye"))
        r.runner.request_immediate_render()
    assert r.terminal.restored


async def test_stopping_parks_the_cursor_below_the_document() -> None:
    """So the shell prompt lands after the transcript, not on top of it."""
    r = Runner()
    async with r:
        r.root.add(Text("last"))
        r.runner.request_immediate_render()
    assert r.output.endswith("\x1b[?25h")
