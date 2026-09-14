"""Building a session for a test, without a terminal.

Shared by the frontend tests. Nothing here knows about a rendering library: it
assembles an :class:`~hx.core.loop.AgentLoop` over a scripted provider, which
is what every behavioural test in this directory actually needs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pyte

from hx.config import load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.session import new_session
from hx.providers.fake import FakeProvider, text_turn
from hx.providers.models import ModelRegistry
from hx.term.terminal import FakeTerminal
from hx.term.width import strip_ansi
from hx.tools.registry import ToolRegistry
from hx.tui.runtime import HXSession

MODEL = "anthropic/claude-sonnet-4.5"

COLUMNS, ROWS = 80, 24

__all__ = [
    "COLUMNS",
    "MODEL",
    "ROWS",
    "Driver",
    "FakeProvider",
    "build_session",
    "gpt5",
    "text_turn",
]


def gpt5() -> Any:
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


def build_session(
    tmp_path: Path,
    script: list[Any] | None = None,
    **extra: Any,
) -> HXSession:
    bus = EventBus()
    models = ModelRegistry()
    session = new_session(tmp_path, MODEL)
    # Already named, so a turn here is one provider call and not two - session
    # naming has its own tests.
    session.set_title("test session")
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
    extra.setdefault("models", models)
    return HXSession(
        loop,
        bus,
        load_settings(tmp_path),
        terminal=FakeTerminal(COLUMNS, ROWS),
        **extra,
    )


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
        """Let the loop, the event consumer and the renderer catch up."""
        for _ in range(rounds):
            await asyncio.sleep(0.01)

    def type(self, text: str) -> None:
        self.terminal.feed(text)

    def display(self) -> list[str]:
        """The non-blank lines a terminal of this size would be showing."""
        return [line for line in self.screen() if line.strip()]

    def screen(self) -> list[str]:
        """Every row of the terminal, blank ones included.

        Where a line sits is the assertion for anything that pins something to
        a row - the dock in fullscreen - so the blanks have to survive.
        """
        screen = pyte.Screen(COLUMNS, ROWS)
        pyte.Stream(screen).feed(self.terminal.output)
        return [line.rstrip() for line in screen.display]

    def screen_text(self) -> str:
        return "\n".join(self.screen())

    def transcript_text(self) -> str:
        """Everything in the document, styling stripped.

        Unlike :meth:`display` this is not limited to what fits on screen, so a
        long reply can be asserted on without the assertion depending on the
        terminal's height.
        """
        return "\n".join(
            strip_ansi(line)
            for block in self.session.view.transcript.blocks
            for line in block.render(COLUMNS)
        )
