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
        self._queued: list[str] = []
        self._mouse = False
        self.commands: Any = None
        """The slash-command registry, built on start."""
        self._silent: set[str] = set()
        self._assistant: AssistantMessage | None = None
        self._thinking: ThinkingMessage | None = None
        self._turn_started = 0.0
        self._turn: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        from hx.tui.commands import CommandContext, build_default_commands

        self.commands = build_default_commands()
        self.prompt.commands = self.commands
        self._context = CommandContext(
            app=self, settings=self.settings, session=self.loop.session, registry=self.commands
        )

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

    # -- the surface the slash commands expect -----------------------------
    #
    # Commands are written against an app object, not against a frontend. Every
    # name below exists because a command asks for it, and they are gathered
    # here rather than scattered so the contract is legible in one place.

    @property
    def status(self) -> Any:
        return self.view.dock.status

    @property
    def models(self) -> Any:
        return self.extra.get("models")

    @property
    def auth(self) -> Any:
        return self.extra.get("auth")

    @property
    def skills(self) -> Any:
        return self.extra.get("skills")

    @property
    def agents(self) -> Any:
        return self.extra.get("agents")

    @property
    def mcp(self) -> Any:
        return self.extra.get("mcp")

    @property
    def sandbox_active(self) -> bool:
        return bool(self.extra.get("sandbox_active", True))

    @property
    def sandbox_backend(self) -> str:
        return str(self.extra.get("sandbox_backend") or "")

    @property
    def mode(self) -> str:
        if self.loop.permissions is None:
            return "default"
        return str(self.loop.permissions.mode.value)

    @property
    def queued(self) -> list[str]:
        return self._queued

    @property
    def last_message_text(self) -> str:
        block = self.view.transcript.last()
        return getattr(block, "text", "") or ""

    @property
    def last_context(self) -> Any:
        return getattr(self.loop.session, "usage", None)

    def notice(self, text: str, level: str = "info") -> None:
        self._notice(text, level)

    def show(self, component: Any) -> None:
        """Put something on the overlay without waiting for it.

        For a flow that drives its own screen and decides when it is finished.
        """
        self.view.show(component)
        self.runner.request_immediate_render()

    def dismiss_modal(self) -> None:
        self.view.dismiss()
        self.runner.request_immediate_render()

    def exit(self) -> None:
        self.runner.stop()

    def set_mode(self, mode: str) -> None:
        from hx.config import PermissionMode

        if self.loop.permissions is not None:
            self.loop.permissions.mode = PermissionMode(mode)
        self.status.set_mode(mode, self.sandbox_active, self.sandbox_backend)
        self.runner.request_render()

    def apply_theme(self, name: str) -> None:
        from hx.tui.theme import THEME

        THEME.use(name)
        # Every cached line holds the old colours, so the document is redrawn
        # from scratch rather than diffed against them.
        self._invalidate_all(self.view)
        self.runner.request_immediate_render()

    def _invalidate_all(self, component: Any) -> None:
        invalidate = getattr(component, "invalidate", None)
        if invalidate is not None:
            invalidate()
        for child in getattr(component, "children", []) or []:
            self._invalidate_all(child)

    async def copy(self, text: str) -> str:
        """Copy, returning which mechanism took it.

        The OSC 52 writer is the terminal itself, which is the fallback that
        works over ssh where no local clipboard command exists.
        """
        from hx.tui.clipboard import copy_text

        return await copy_text(text, write_osc52=self._write_osc52)

    def _write_osc52(self, payload: str) -> None:
        self.runner.terminal.write(payload)

    def set_mouse_reporting(self, enabled: bool) -> None:
        setter = getattr(self.runner.terminal, "set_mouse", None)
        if setter is not None:
            setter(enabled)
        self._mouse = enabled

    @property
    def mouse_reporting(self) -> bool:
        return self._mouse

    async def submit_to_model(self, text: str) -> None:
        self._submit(text)

    def show_todos(self) -> None:
        """Re-emit the current plan as a transcript block."""
        todos = getattr(self.loop.session, "todos", None)
        if not todos:
            self._notice("No plan yet.", "warning")
            return
        self.view.transcript.append(TodoBlock(todos))
        self.runner.request_render()

    def clear_queue(self) -> None:
        self._queued.clear()

    def steer_queued(self) -> None:
        for text in self._queued:
            self.loop.steer(text)
        self._queued.clear()

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
            await self._run_command(f"/{chosen}")

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
        return list(self.commands.all()) if self.commands is not None else []

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
        if name == "ctrl+t":
            self.show_todos()
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
        if text.startswith("/"):
            self._turn = asyncio.create_task(self._run_command(text))
            return
        if self.is_busy:
            # Held rather than refused: the user typed it while a turn was
            # running, and dropping it loses the message.
            self._queued.append(text)
            self.status.update(queued=len(self._queued))
            return
        self._turn = asyncio.create_task(self._run_turn(text))

    async def _run_command(self, line: str) -> None:
        try:
            await self.commands.dispatch(self._context, line)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._notice(str(error), "error")
        finally:
            self.runner.request_immediate_render()

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
