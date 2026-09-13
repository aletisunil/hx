"""Running an interactive session.

Events arrive on the bus and are dispatched to components. The session owns the
turn, the queue, the overlay, and whichever approval currently holds the
keyboard - and it is the object the slash commands are written against, so the
surface they may ask for is gathered in one block rather than scattered.
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
        self._background: set[asyncio.Task[Any]] = set()
        """Detached work - session renaming - held so it is not garbage
        collected mid-flight."""
        self._mouse = False
        self._clear_armed = False
        """Set by one ctrl+c on an empty, idle prompt; a second one exits."""
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
        self._command_context = CommandContext(
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
        """The credential resolver.

        Defaulted rather than left empty: a session started without one - a
        test, a stand-alone run - still has to be able to switch routes, and a
        missing resolver surfaces as an attribute error deep inside the
        provider registry rather than as anything a user could act on.
        """
        resolver = self.extra.get("auth")
        if resolver is None:
            from hx.auth.resolve import AuthResolver

            resolver = AuthResolver()
            self.extra["auth"] = resolver
        return resolver

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

    def dismiss_modal(self, component: Any = None) -> None:
        """Take the overlay down.

        The component is accepted and ignored: callers pass the one they put
        up, and only one thing is ever on the overlay. Refusing the argument
        raised inside a ``finally``, which left the dialog on screen after the
        flow behind it had already failed.
        """
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

    @property
    def checkpoints(self) -> Any:
        return self.extra.get("checkpoints")

    @property
    def tracker(self) -> Any:
        return self.extra.get("tracker")

    def set_api_key(self, key: str) -> None:
        """Apply a new credential to the running provider.

        Saved keys are picked up on the next start; this is what makes the
        change take effect now, without losing the session.
        """
        setter = getattr(getattr(self.loop, "provider", None), "set_api_key", None)
        if setter is not None:
            setter(key)

    async def use_route_for(self, model_id: str) -> None:
        """Point the session at whichever provider serves ``model_id``.

        A model id carries its route, so switching from an OpenRouter model to
        a subscription one has to replace the provider - not just the model
        name, which would send a Codex id to OpenRouter.

        Routes are compared by model id rather than by the live provider's
        name, so an injected or wrapped provider is left alone as long as the
        route has not actually changed.
        """
        from hx.providers import registry

        spec = registry.provider_for(model_id)
        if spec.id == registry.provider_for(self.loop.model).id:
            return

        provider = registry.build_provider(
            model_id,
            self.auth,
            session_id=self.loop.session.meta.session_id,
            models=self.models,
        )
        previous = self.loop.set_provider(provider)
        self._retarget_subagents(provider)
        with contextlib.suppress(Exception):
            await previous.aclose()

    def _retarget_subagents(self, provider: Any) -> None:
        """Subagents share the parent's provider; they must follow the switch."""
        with contextlib.suppress(Exception):
            task_tool = self.loop.tools.get("Task")
            setter = getattr(task_tool, "set_provider", None)
            if setter is not None:
                setter(provider)

    def rename_outgoing_session(self, session: Any) -> None:
        """Name the session being left behind for what it ended up being.

        Detached: the user asked for a new session, not to wait on a name for
        the old one. It lands in that session's meta.json when it arrives,
        which is all /resume reads.
        """
        if not session.messages:
            return
        task = asyncio.create_task(self.loop.retitle_session(session))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def start_new_session(self) -> None:
        """Fresh transcript, same directory. The old session stays on disk."""
        from hx.core.session import new_session

        self.rename_outgoing_session(self.loop.session)
        self.loop.session = new_session(self.settings.cwd, self.loop.model)
        # The session id is the prompt-cache key on routes that use one; a
        # stale id would keep the new conversation hitting the old cache.
        setter = getattr(self.loop.provider, "set_session_id", None)
        if setter is not None:
            setter(self.loop.session.meta.session_id)
        self._attach_checkpoints()
        self.view.transcript.clear()
        self._notice("New session started.", "success")
        window = self.loop.model_info.context_window if self.loop.model_info else 0
        self.status.set_tokens(0, 0)
        self.status.set_cache(0, 0, 0.0)
        self.status.update(cost_usd=0.0)
        self.status.set_context(0, window)
        self.runner.request_immediate_render()

    def resume_session(self, session_id: str) -> None:
        from hx.core.session import load_session

        try:
            session = load_session(session_id)
        except Exception as exc:
            self._notice(f"Could not resume {session_id}: {exc}", "error")
            return

        self.rename_outgoing_session(self.loop.session)
        self.loop.session = session
        setter = getattr(self.loop.provider, "set_session_id", None)
        if setter is not None:
            setter(session.meta.session_id)
        self._attach_checkpoints()
        self._replay_transcript()
        self._notice(f"Resumed {session_id}.", "success")
        self.runner.request_immediate_render()

    def rewind_to(self, index: int) -> Any:
        """Take the session back to just before message ``index``.

        Files first, then the transcript: if the restore fails the session
        still describes the tree as it actually is. The prompt that was cut is
        handed back to the input rather than dropped - a rewind is nearly
        always the first half of "say that differently".
        """
        session = self.loop.session
        prompt = session.messages[index].text() if index < len(session.messages) else ""
        report = None
        if self.checkpoints is not None:
            report = self.checkpoints.restore_to(index, self.tracker)
        session.rewind_to(index)
        self._replay_transcript()
        if prompt:
            self.prompt.text = prompt
            self.prompt.buffer.cursor = len(prompt)
        self.runner.request_immediate_render()
        return report

    def _attach_checkpoints(self) -> None:
        """Point the store at whichever session the tools are now writing to."""
        if self.checkpoints is None:
            return
        from hx.paths import session_checkpoints_dir

        session = self.loop.session
        self.checkpoints.attach(session, session_checkpoints_dir(session.meta.session_id))

    def _replay_transcript(self) -> None:
        """Redraw the transcript from the session.

        Rebuilt rather than patched: the screen no longer matches the history,
        and there is no reliable way to reconcile the two.
        """
        transcript = self.view.transcript
        transcript.clear()
        self._assistant = None
        self._thinking = None
        for message in self.loop.session.active_messages():
            if message.role == "user":
                transcript.append(UserMessage(message.text()))
            elif text := message.text():
                transcript.append(AssistantMessage(text))

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

    def _clear_or_exit(self) -> None:
        """Clear the draft; on an empty prompt, interrupt; then exit.

        A draft takes priority so it can be cleared without interrupting the
        agent. With nothing typed and a turn running, the key means what every
        terminal user expects. With neither, a second press exits - announced
        first, because one keystroke should not end a session.
        """
        if self.prompt.value:
            self.prompt.clear()
            self._clear_armed = False
            self.runner.request_immediate_render()
            return
        if self.is_busy:
            self.loop.cancel()
            if self._turn is not None:
                self._turn.cancel()
            return
        if self._clear_armed:
            self.runner.stop()
            return
        self._clear_armed = True
        from hx.keys import KEYMAP

        self._notice(f"Press {KEYMAP.text('app.clear')} again to exit.")

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
        from hx.keys import KEYMAP

        name = key.name
        if name != "ctrl+c":
            # Any other key means the user is still working, so the pending
            # exit is no longer what a second ctrl+c should mean.
            self._clear_armed = False

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
            self._clear_or_exit()
            return
        if name == "shift+tab":
            self.cycle_mode()
            return
        if name in KEYMAP.keys_for("app.transcript.previousPrompt"):
            self.view.transcript.move_cursor(-1)
            self.runner.request_immediate_render()
            return
        if name in KEYMAP.keys_for("app.transcript.nextPrompt"):
            self.view.transcript.move_cursor(1)
            self.runner.request_immediate_render()
            return
        if name in KEYMAP.keys_for("app.message.copy"):
            asyncio.create_task(self.copy_cursored())  # noqa: RUF006
            return
        if name in KEYMAP.keys_for("app.tools.expand"):
            if self.view.transcript.blocks:
                self.expand_cursored()
            else:
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
        if text.startswith("!"):
            self._turn = asyncio.create_task(self.run_shell_passthrough(text[1:].strip()))
            return
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

    async def run_shell_passthrough(self, command: str) -> None:
        """``!command`` - run a shell command directly, without a model turn.

        It goes through the same permission engine and sandbox as a
        model-issued command; a shortcut that skipped those would be a hole in
        both.
        """
        if not command:
            return
        from hx.tools.base import ToolContext

        self.view.transcript.append(UserMessage(f"!{command}"))
        self.runner.request_immediate_render()

        if not self.loop.tools.has("Bash"):
            self._notice("No shell is attached to this session.", "error")
            return
        if not await self._permit_shell(command):
            self._notice(f"Refused: !{command}", "warning")
            return

        block = ToolBlock(
            ToolCall(name="Bash", params={"command": command}, cwd=Path(self.settings.cwd))
        )
        self.view.transcript.append(block)
        self.runner.request_immediate_render()

        context = ToolContext(
            cwd=self.settings.cwd,
            session_id=self.loop.session.meta.session_id,
            tool_use_id=f"shell_{id(command):x}",
            settings=self.settings,
            emit_progress=lambda chunk: self._append_output(block, chunk),
        )
        result = await self.loop.tools.call("Bash", {"command": command}, context)
        block.update(
            finished=True,
            is_error=result.is_error,
            summary=result.summary or "",
            output=result.content or "",
        )
        self.runner.request_immediate_render()

    def _append_output(self, block: ToolBlock, chunk: str) -> None:
        block.update(output=block.call.output + chunk)
        self.runner.request_render()

    async def _permit_shell(self, command: str) -> bool:
        from hx.permissions.engine import PermissionRequest

        if self.loop.permissions is None:
            return True
        allowed, _reason = await self.loop.permissions.request(
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
        return allowed

    def cycle_mode(self) -> None:
        from hx.config import PermissionMode

        order = list(PermissionMode)
        current = PermissionMode(self.mode)
        self.set_mode(order[(order.index(current) + 1) % len(order)].value)

    async def copy_cursored(self) -> None:
        """Copy the cursored message, or the last answer when none is cursored."""
        text = self.view.transcript.cursored_text()
        if not text:
            self._notice("Nothing to copy yet.", "warning")
            return
        try:
            used = await self.copy(text)
        except Exception as error:
            self._notice(f"Could not copy: {error}", "error")
            return
        self._notice(f"Copied to the clipboard ({used}).", "success")

    def expand_cursored(self) -> None:
        """Expand the cursored block, or every tool block when none is."""
        cursor = self.view.transcript.cursor
        if cursor is not None and hasattr(cursor, "toggle"):
            cursor.toggle()
        else:
            for block in self.view.transcript.blocks:
                if isinstance(block, ToolBlock):
                    block.toggle()
        self.runner.request_immediate_render()

    async def _run_command(self, line: str) -> None:
        try:
            await self.commands.dispatch(self._command_context, line)
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


async def run_session(loop: AgentLoop, bus: EventBus, settings: Settings, **kwargs: Any) -> None:
    """Run one interactive session to completion."""
    import sys

    if sys.platform == "win32":
        raise RuntimeError(
            "HX's terminal interface needs a POSIX terminal. On Windows, run it under WSL."
        )
    await HXSession(loop, bus, settings, **kwargs).run()
