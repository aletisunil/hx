"""Running a session on the scrollback-native renderer.

The same event bus the Textual app consumes, dispatched to components instead
of widgets. The match/case below is deliberately the same shape as the one in
:mod:`hx.tui.legacy.app`, because it is the part that carries over unchanged
and keeping it recognisable is what makes the two comparable while both exist.

Read-only for now: the prompt is a stub that types a line and submits it, and
the real editor, the dialogs and the slash commands land in later stages. What
this is for is proving the renderer against real sessions - streaming deltas,
long transcripts, live resizes - before more is built on top of it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hx.core import events as ev
from hx.core.usage import format_tokens
from hx.term.loop import TuiRunner
from hx.term.terminal import Terminal
from hx.tui.renderers import ToolCall
from hx.tui.views.blocks import (
    AssistantMessage,
    Notice,
    ThinkingMessage,
    TodoBlock,
    ToolBlock,
    UserMessage,
)
from hx.tui.views.permission import PermissionPrompt
from hx.tui.views.prompt import Prompt
from hx.tui.views.transcript import Session

if TYPE_CHECKING:
    from hx.config import Settings
    from hx.core.events import EventBus
    from hx.core.loop import AgentLoop

SPINNER_INTERVAL = 0.08
"""Seconds between spinner frames. Fast enough to read as motion, slow enough
that it is not the thing the renderer spends its budget on."""

SILENT_TOOLS = frozenset({"todowrite"})
"""Tools whose own block is suppressed because something else draws the result.

Only TodoWrite so far. Its renderer prints the whole plan and so does the
:class:`~hx.tui.views.blocks.TodoBlock` that ``TodosUpdated`` appends a moment
later, which in the Textual app were two regions - an inline call and a sidebar
- and here are two blocks in the same column. A failed call publishes no
``TodosUpdated``, so the held block is appended then; see ``ToolCallFinished``.
"""

HINTS = [
    ("esc", "interrupt"),
    ("ctrl+c", "clear"),
    ("ctrl+d", "exit"),
    ("ctrl+o", "expand"),
    ("/", "commands"),
    ("!", "bash"),
    ("@", "files"),
]


class HXSession:
    """One interactive session, drawn by :mod:`hx.term`."""

    def __init__(
        self,
        loop: AgentLoop,
        bus: EventBus,
        settings: Settings,
        terminal: Terminal | None = None,
        **extra: Any,
    ) -> None:
        self.loop = loop
        self.bus = bus
        self.settings = settings
        self.extra = extra

        from hx import __version__

        self.prompt = Prompt(
            Path(settings.cwd),
            commands=extra.get("commands"),
            rows_available=self._terminal_rows,
            on_submit=self._submit,
            on_steer=self._steer,
        )
        self.view = Session(__version__, quiet=settings.quiet_startup, prompt=self.prompt)
        # Injectable so a test can drive a whole session without a tty, and
        # read back what a terminal would have shown.
        self.runner = TuiRunner(self.view, terminal, on_key=self._on_key)

        self._tools: dict[str, ToolBlock] = {}
        self._pending: PermissionPrompt | None = None
        """The approval the keyboard is currently answering, if any."""
        self._overlay_finished: asyncio.Event | None = None
        """Set when whatever is on the overlay reports that it is done."""
        self._silent: set[str] = set()
        self._assistant: AssistantMessage | None = None
        self._thinking: ThinkingMessage | None = None
        self._turn_started = 0.0
        self._turn: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        self.view.dock.hints.set_hints(HINTS)
        self._refresh_status()
        if self.loop.permissions is not None:
            self.loop.permissions.asker = self.ask_permission

        events = asyncio.create_task(self._consume_events())
        spinner = asyncio.create_task(self._spin())
        try:
            await self.runner.run()
        finally:
            for task in (events, spinner, self._turn):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def _spin(self) -> None:
        while True:
            await asyncio.sleep(SPINNER_INTERVAL)
            if not self._turn_started:
                continue
            elapsed = time.monotonic() - self._turn_started
            self.view.dock.working.tick(elapsed)
            for block in self._tools.values():
                block.tick()
            self.runner.request_render()

    def _terminal_rows(self) -> int:
        """How tall the terminal is, so the draft never eats the screen."""
        return self.runner.terminal.size[1]

    @property
    def _enter_steers(self) -> bool:
        from hx.config import EnterWhileBusy

        return self.settings.tui.enter_while_busy is EnterWhileBusy.STEER

    @property
    def is_busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    # -- input -------------------------------------------------------------

    async def _open_commands(self) -> None:
        from hx.tui.views.pickers import CommandPalette

        chosen = await self.ask(CommandPalette(self._commands()))
        if chosen:
            self._notice(f"/{chosen}")

    async def _open_models(self) -> None:
        from hx.tui.views.pickers import ModelPicker

        registry = self.extra.get("models")
        if registry is None:
            self._notice("no model registry is loaded", "warning")
            return
        current = getattr(getattr(self.loop, "model_info", None), "id", "") or ""
        chosen = await self.ask(ModelPicker(list(registry.all()), current))
        if chosen:
            self.view.dock.status.set_model(chosen)
            self._notice(f"model set to {chosen}")

    def _commands(self) -> list[Any]:
        """The slash commands this session offers.

        Empty until the command registry is ported, which is the next stage -
        the palette itself is complete and its rows come from here.
        """
        return list(self.extra.get("commands") or [])

    async def ask(self, component: Any) -> Any:
        """Show ``component`` and wait for it to finish.

        The component owns the keyboard until its ``done`` flag is set, then
        its ``result`` is returned. That is the whole contract, which is what
        lets a picker be written as a component rather than as a screen with a
        lifecycle.
        """
        finished = asyncio.Event()
        self._overlay_finished = finished
        self.view.show(component)
        self.runner.request_immediate_render()
        try:
            await finished.wait()
            return getattr(component, "result", None)
        finally:
            self._overlay_finished = None
            self.view.dismiss()
            self.runner.request_immediate_render()

    async def ask_permission(self, request: Any) -> Any:
        """Ask in the transcript and wait for the answer.

        Inline rather than over the top: the model has just said what it
        intends to do, and covering that sentence at the moment the user has to
        judge it is the wrong trade. Answering leaves the block behind as a
        record of what was granted.
        """
        from hx.permissions.engine import PermissionAnswer

        future: asyncio.Future[PermissionAnswer] = asyncio.get_running_loop().create_future()
        prompt = PermissionPrompt(
            request,
            future,
            origin=getattr(request, "origin", None),
            cwd=Path(self.settings.cwd),
        )
        self.view.transcript.append(prompt)
        self._pending = prompt
        # The turn is blocked on a person, so the indicator must stop claiming
        # the tool is running.
        self.view.dock.working.stop()
        self.runner.request_immediate_render()
        try:
            return await future
        except asyncio.CancelledError:
            # An interrupt landed while this was still open. Nobody answered
            # it, and a block that goes on offering keys that reach nothing is
            # worse than one that says so.
            prompt.abandon()
            raise
        finally:
            self._pending = None
            self.view.dock.working.start()
            self.runner.request_immediate_render()

    def _on_key(self, key: Any) -> None:
        name = key.name

        # An open approval owns the keyboard: it is the one thing on screen
        # waiting on the user, and "y" must not be typed into the prompt.
        pending = self._pending
        if pending is not None and not pending.answered and pending.handle_input(name, key.data):
            self.runner.request_immediate_render()
            return

        # So does an overlay, and it takes precedence over every app binding
        # below - ctrl+o inside a picker is that picker's business.
        showing = self.view.showing
        if showing is not None:
            self.view.handle_input(name, key.data)
            if getattr(showing, "done", False) and self._overlay_finished is not None:
                self._overlay_finished.set()
            self.runner.request_immediate_render()
            return

        if name == "ctrl+d" and not self.view.dock.prompt.value:
            self.runner.stop()
            return
        if name == "escape" and self.is_busy:
            # Both halves, as the old app does: the loop stops asking for more,
            # and the task running the turn is cancelled so an in-flight tool
            # does not carry on after the user said stop.
            self.loop.cancel()
            if self._turn is not None:
                self._turn.cancel()
            return
        if name == "ctrl+c":
            if self.view.dock.prompt.value:
                self.view.dock.prompt.clear()
            else:
                self.runner.stop()
            return
        if name == "ctrl+o":
            self.view.header.toggle()
            return
        if name == "ctrl+p":
            asyncio.create_task(self._open_commands())  # noqa: RUF006
            return
        if name == "ctrl+l":
            asyncio.create_task(self._open_models())  # noqa: RUF006
            return
        self.view.handle_input(name, key.data)

    def _submit(self, text: str) -> None:
        self.view.transcript.append(UserMessage(text))
        if self.is_busy:
            self._notice("a turn is already running", "warning")
            return
        self._turn = asyncio.create_task(self._run_turn(text))

    def _steer(self, text: str) -> None:
        """Alt+Enter: put this into the running turn now.

        With nothing in flight there is no tail to drain a queue, so a steer
        with no turn running is just a submission - otherwise the message sits
        there being described as waiting on a turn that does not exist.
        """
        if not self.is_busy:
            if text:
                self._submit(text)
            return
        self.view.transcript.append(UserMessage(text))
        self.loop.steer(text)

    async def _run_turn(self, text: str) -> None:
        try:
            await self.loop.run(text)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._notice(str(error), "error")
        finally:
            self._turn_started = 0.0
            self.view.dock.working.stop()
            self.runner.request_immediate_render()

    # -- events ------------------------------------------------------------

    def _notice(self, text: str, level: str = "info") -> None:
        self.view.transcript.append(Notice(text, level))
        self.runner.request_render()

    async def _consume_events(self) -> None:
        transcript = self.view.transcript
        status = self.view.dock.status
        working = self.view.dock.working

        async for event in self.bus.subscribe():
            match event:
                case ev.TurnStarted():
                    self._assistant = None
                    self._thinking = None
                    self._turn_started = time.monotonic()
                    working.start()
                    self.prompt.set_running(True, enter_steers=self._enter_steers)
                case ev.TextDelta():
                    if self._assistant is None:
                        self._assistant = AssistantMessage()
                        transcript.append(self._assistant)
                    self._assistant.append(event.text)
                case ev.ThinkingDelta():
                    if self._thinking is None:
                        self._thinking = ThinkingMessage()
                        transcript.append(self._thinking)
                    self._thinking.append(event.text)
                case ev.ToolCallStarted():
                    started = ToolBlock(
                        ToolCall(
                            name=event.name,
                            params=dict(event.input or {}),
                            cwd=Path(self.settings.cwd),
                        )
                    )
                    self._tools[event.tool_use_id] = started
                    # A silent tool is held back rather than dropped: it is
                    # silent because something else draws its result, and if it
                    # fails there is no result and nothing else to draw. The
                    # block is appended on failure instead.
                    if event.name.lower() in SILENT_TOOLS:
                        self._silent.add(event.tool_use_id)
                    else:
                        transcript.append(started)
                    self._assistant = None
                case ev.ToolCallFinished():
                    finished = self._tools.pop(event.tool_use_id, None)
                    held = event.tool_use_id in self._silent
                    self._silent.discard(event.tool_use_id)
                    if finished is not None:
                        finished.update(
                            finished=True,
                            is_error=event.is_error,
                            summary=event.summary or "",
                            output=event.detail or "",
                            metadata=event.metadata,
                            duration_ms=event.duration_ms or 0.0,
                        )
                        if held and event.is_error:
                            transcript.append(finished)
                case ev.UsageUpdated():
                    status.set_tokens(event.input_tokens, event.output_tokens)
                    status.set_context(event.context_tokens, event.context_window)
                    status.set_cache(
                        event.cache_read_tokens,
                        event.cache_write_tokens,
                        self.loop.session.usage.cache_hit_rate,
                    )
                    status.update(
                        cost_usd=event.cost_usd,
                        latency_ms=self.loop.session.usage.last_latency_ms,
                    )
                case ev.TodosUpdated():
                    transcript.append(TodoBlock(event.todos))
                case ev.CompactionStarted():
                    self._notice(f"Compacting ({event.reason})…")
                case ev.CompactionFinished():
                    saved = event.tokens_before - event.tokens_after
                    if saved > 0:
                        self._notice(
                            f"Compacted {format_tokens(event.tokens_before)} → "
                            f"{format_tokens(event.tokens_after)} tokens. The cached "
                            "conversation is discarded, so the next turn re-reads it "
                            "at full price."
                        )
                    status.set_context(event.tokens_after, status.context_window)
                case ev.ErrorRaised():
                    self._notice(event.message, "error")
                case ev.TurnFinished():
                    self._turn_started = 0.0
                    working.stop()
                    self.prompt.set_running(False)

            self.runner.request_render()

    def _refresh_status(self) -> None:
        status = self.view.dock.status
        status.set_location(_tilde(Path(self.settings.cwd)), None)
        status.set_mode(
            str(self.settings.permissions.mode),
            self.extra.get("sandbox_active", True),
            str(self.extra.get("sandbox_backend") or ""),
        )
        model = getattr(self.loop, "model_info", None)
        if model is not None:
            status.set_model(getattr(model, "id", "") or "")
            status.set_context(0, getattr(model, "context_window", 0) or 0)


def _tilde(path: Path) -> str:
    try:
        return f"~/{path.resolve().relative_to(Path.home())}"
    except ValueError:
        return str(path)


async def run_new_tui(loop: AgentLoop, bus: EventBus, settings: Settings, **kwargs: Any) -> None:
    """Entry point for ``tui.renderer = "new"``."""
    import sys

    if sys.platform == "win32":
        raise RuntimeError(
            "the scrollback renderer needs a POSIX terminal. On Windows, run HX under WSL."
        )
    await HXSession(loop, bus, settings, **kwargs).run()
