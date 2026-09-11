"""The HX Textual application.

Layout::

    +--------------------------------------------------+
    |  transcript (streaming markdown, tool blocks)     |  todo
    |                                                   |  side
    |                                                   |  bar
    +--------------------------------------------------+------+
    |  input (multiline, @file completion, ! passthrough)      |
    +----------------------------------------------------------+
    |  status bar: model | ctx | tokens | cache | $ | mode      |
    +----------------------------------------------------------+

The app owns no agent state. It subscribes to the event bus and renders; user
actions are pushed back into the loop as messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal

from hx.core import events as ev
from hx.core.usage import format_tokens
from hx.git import BranchWatcher
from hx.keys import KEYMAP, bindings_for
from hx.providers.models import ModelRegistry
from hx.tui.commands import CommandContext, CommandRegistry, build_default_commands
from hx.tui.theme import THEME, textual_theme
from hx.tui.widgets.autocomplete import Autocomplete
from hx.tui.widgets.frame import BottomRule, PromptFrame
from hx.tui.widgets.hints import HintsBar
from hx.tui.widgets.input import PromptInput
from hx.tui.widgets.statusbar import StatusBar
from hx.tui.widgets.todos import SubagentRows, TodoSidebar
from hx.tui.widgets.transcript import Transcript
from hx.tui.widgets.working import WorkingIndicator

if TYPE_CHECKING:
    from hx.config import Settings
    from hx.core.events import EventBus
    from hx.core.loop import AgentLoop


BRANCH_POLL_SECONDS = 1.0
"""How often the status bar re-reads ``HEAD``. One stat, so this is cheap; a
second of lag after a checkout is below the threshold of noticing."""


class HXApp(App[None]):
    """Top-level Textual app."""

    CSS_PATH = "hx.tcss"
    ENABLE_COMMAND_PALETTE = False
    """Textual's built-in palette would shadow ctrl+p; HX has its own."""
    #: Built from the keybinding registry, so the keys here, the ones ``/help``
    #: prints and the ones the hints bar shows cannot drift apart.
    BINDINGS: ClassVar[list[BindingType]] = bindings_for(  # type: ignore[assignment]
        "app.interrupt",
        "app.selection.copy",
        "app.clear",
        "app.exit",
        "app.suspend",
        "app.mode.cycle",
        "app.commands",
        "app.model.select",
        "app.tools.expand",
        "app.todos.toggle",
        "app.message.copy",
        "app.transcript.pageUp",
        "app.transcript.pageDown",
        "app.transcript.top",
        "app.transcript.bottom",
        "app.transcript.previousPrompt",
        "app.transcript.nextPrompt",
    )

    def __init__(
        self,
        loop: AgentLoop,
        bus: EventBus,
        settings: Settings,
        models: ModelRegistry | None = None,
        auth: Any = None,
        sandbox_active: bool = True,
        sandbox_backend: str = "none",
        skills: Any = None,
        agents: Any = None,
        mcp: Any = None,
        notices: list[str] | None = None,
        checkpoints: Any = None,
        tracker: Any = None,
    ) -> None:
        super().__init__()
        self._startup_notices = list(notices or ())
        self.sandbox_active = sandbox_active
        self._sandbox_backend = sandbox_backend
        self.skills = skills or {}
        self.agents = agents or {}
        self.mcp = mcp
        self.checkpoints = checkpoints
        """:class:`~hx.core.checkpoints.CheckpointStore` - what ``/rewind`` restores."""
        self.tracker = tracker
        self.loop = loop
        self.models = models if models is not None else ModelRegistry()
        self.auth = auth if auth is not None else _default_auth()
        self.bus = bus
        self.settings = settings
        self.commands: CommandRegistry = build_default_commands()
        # Bound in on_mount; see the note there on screen-scoped queries.
        self._transcript: Transcript
        self._status: StatusBar
        self._subagents: SubagentRows
        self._todos: TodoSidebar
        self._prompt: PromptInput
        self._working: WorkingIndicator
        self._bottom_rule: BottomRule
        # Settings are frozen; the live permission mode is session state.
        self.mode = settings.permissions.mode
        # Before anything parses hx.tcss: the stylesheet reads variables that
        # only exist once an HX theme is installed, and Textual parses CSS on
        # the way to the first frame, well before on_mount runs.
        self.apply_theme(settings.theme)
        self.mouse_reporting = True
        """Whether the terminal is reporting mouse events to HX. ``/mouse off``
        hands drag-selection back to the terminal."""
        self._branch = BranchWatcher(settings.cwd)
        self._turn_worker: Any = None
        self._queued: list[str] = []
        self._already_echoed: list[str] = []
        """Steers shown in the transcript before an interrupt sent them back to
        the queue. Replaying one must not echo it a second time."""
        #: Set by a ctrl+c on an empty prompt; a second press then exits.
        self._clear_armed = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield Transcript(self.settings.cwd)
            yield TodoSidebar()
        yield SubagentRows()
        yield Autocomplete()
        yield PromptFrame(self.settings.cwd)
        yield HintsBar()
        yield StatusBar()

    async def on_mount(self) -> None:
        """Start the event-bus consumer task and cache the main widgets.

        ``query_one`` resolves against the *active* screen, so every lookup
        would fail while any modal is up - killing whichever worker
        made it. The main screen's widgets are therefore looked up once, here,
        and referenced directly from then on.
        """
        # Applied in __init__ as well, for the stylesheet's sake; repeated here
        # because settings can be swapped between construction and mount.
        self.apply_theme(self.settings.theme)

        self._transcript = self.query_one(Transcript)
        self._status = self.query_one(StatusBar)
        self._subagents = self.query_one(SubagentRows)
        self._todos = self.query_one(TodoSidebar)
        self._prompt = self.query_one(PromptInput)
        self._working = self.query_one(WorkingIndicator)
        self._bottom_rule = self.query_one(BottomRule)

        status = self._status
        status.set_model(
            self.loop.model,
            subscription=self.models.get_or_default(self.loop.model).is_subscription,
        )
        status.set_effort(self.models.displayed_effort(self.loop.model))
        status.set_mode(self.mode.value, self.sandbox_active, self._sandbox_backend)
        self._refresh_branch()
        # Checking out happens in another terminal or the IDE as often as it
        # does here, and a status bar that answered "which branch" once at
        # startup was wrong for the rest of the session. The watcher stats one
        # file, so this costs nothing between checkouts.
        self.set_interval(BRANCH_POLL_SECONDS, self._refresh_branch)
        if self.loop.model_info is not None:
            status.set_context(0, self.loop.model_info.context_window)

        if self.loop.permissions is not None:
            self.loop.permissions.asker = self.ask_permission
            status.set_mode(
                self.loop.permissions.mode.value, self.sandbox_active, self._sandbox_backend
            )

        if not self.settings.quiet_startup:
            from hx import __version__
            from hx.tui.widgets.header import StartupHeader

            self._header = StartupHeader(__version__)
            self._transcript.mount(self._header)

        for problem in KEYMAP.problems:
            self._transcript.add_notice(f"Keybindings: {problem}", "warning")

        for notice in self._startup_notices:
            self._transcript.add_notice(notice, "info")

        self._prompt.focus()
        self._sync_frame()
        self.run_worker(self._consume_events(), name="events", exclusive=False)

    async def _consume_events(self) -> None:
        """Drain the bus and dispatch to widgets.

        Text deltas are applied straight through: Textual already coalesces
        repaints on its own refresh tick, so the per-delta cost is a buffer
        append, not a render.
        """
        transcript = self._transcript
        status = self._status
        subagents = self._subagents
        todos = self._todos
        working = self._working

        async for event in self.bus.subscribe():
            match event:
                case ev.TurnStarted():
                    transcript.start_assistant_message()
                    working.start("thinking")
                case ev.TextDelta():
                    transcript.append_delta(event.text)
                case ev.ThinkingDelta():
                    transcript.append_thinking(event.text)
                case ev.ToolCallStarted():
                    transcript.add_tool_block(event.tool_use_id, event.name, event.input)
                    working.start(event.name)
                case ev.ToolCallProgress():
                    transcript.update_tool_block(event.tool_use_id, event.chunk)
                case ev.ToolCallFinished():
                    transcript.finish_tool_block(
                        event.tool_use_id,
                        event.summary,
                        event.is_error,
                        metadata=event.metadata,
                        duration_ms=event.duration_ms,
                        detail=event.detail,
                    )
                    working.start("thinking")
                case ev.UsageUpdated():
                    status.set_tokens(event.input_tokens, event.output_tokens)
                    status.set_context(event.context_tokens, event.context_window)
                    status.set_cache(
                        event.cache_read_tokens,
                        event.cache_write_tokens,
                        self.loop.session.usage.cache_hit_rate,
                    )
                    status.set_cost(event.cost_usd)
                    status.set_latency(self.loop.session.usage.last_latency_ms)
                case ev.TodosUpdated():
                    todos.update_todos(event.todos)
                case ev.SubagentStarted():
                    subagents.start(event.subagent_id, event.agent_type, event.description)
                case ev.SubagentFinished():
                    subagents.finish(event.subagent_id, event.is_error)
                case ev.CompactionStarted():
                    transcript.add_notice(f"Compacting ({event.reason})…", "info")
                    working.start("compacting")
                case ev.CompactionFinished():
                    saved = event.tokens_before - event.tokens_after
                    if saved > 0:
                        transcript.add_notice(
                            f"Compacted {format_tokens(event.tokens_before)} → "
                            f"{format_tokens(event.tokens_after)} tokens. "
                            "The cached conversation is discarded, so the next "
                            "turn re-reads it at full price.",
                            "info",
                        )
                    status.set_context(event.tokens_after, status.context_window)
                case ev.ErrorRaised():
                    transcript.add_notice(event.message, "error")
                case ev.TurnFinished():
                    working.stop()

    async def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        from hx.config import EnterWhileBusy

        if self.is_busy and self.settings.tui.enter_while_busy is EnterWhileBusy.STEER:
            self.steer(message.text)
            return
        await self.submit(message.text)

    async def on_prompt_input_steered(self, message: PromptInput.Steered) -> None:
        """Alt+Enter - the other half of whatever Enter does while busy."""
        from hx.config import EnterWhileBusy

        if self.settings.tui.enter_while_busy is EnterWhileBusy.STEER and message.text:
            # Queueing only means something while a turn is running. With
            # nothing in flight there is no tail to drain the queue, so the
            # message would sit there being described as waiting on a turn that
            # does not exist.
            if self.is_busy:
                self._queue(message.text)
            else:
                await self.submit(message.text)
            return
        self.steer(message.text)

    async def submit(self, text: str) -> None:
        """Handle a user submission: slash command, ``!`` passthrough, or a turn."""
        # Any real activity disarms the pending exit. Otherwise a ctrl+c from
        # an hour ago still counts as the first of two presses, and the next
        # one quits with no warning at all.
        self._clear_armed = False
        if text.strip() == "/":
            await self.action_commands()
            return

        if text.startswith("/"):
            # A command may open a modal, and push_screen_wait is only valid
            # inside worker context; run every command in one so they all
            # behave the same.
            self.run_worker(self._run_command(text), name="command", exclusive=False)
            return

        if text.startswith("!"):
            await self.run_shell_passthrough(text[1:].strip())
            return

        if self.is_busy:
            # Do not interleave turns: queue and run it when the current one ends.
            self._queue(text)
            return

        if text in self._already_echoed:
            self._already_echoed.remove(text)
        else:
            self._transcript.add_user_message(text)
        # A Textual worker, not a bare task: a turn can open a modal, and
        # push_screen_wait is only valid inside worker context.
        self._turn_worker = self.run_worker(self._run_turn(text), name="turn", exclusive=False)

    def _queue(self, text: str) -> None:
        """Hold a message until the running turn ends.

        The notice names the way out, because the queue is exactly where a user
        who has changed their mind ends up: they typed the correction, and now
        it is waiting behind the thing they wanted to correct.
        """
        text = text.strip()
        if not text:
            return
        self._queued.append(text)
        key = KEYMAP.text("tui.input.steer")
        position = len(self._queued)
        self._transcript.add_notice(
            f"queued ({position}) · {key} to steer it into the running turn",
            "info",
        )
        self._sync_queue_depth()

    def steer(self, text: str) -> None:
        """Put a message into the running turn - typed now, or already queued.

        Empty ``text`` promotes the front of the queue, so a message that is
        already waiting does not have to be typed again.
        """
        text = text.strip()
        if not text:
            if not self._queued:
                self.notice("Nothing queued to steer.", "warning")
                return
            text = self._queued.pop(0)
            self._sync_queue_depth()

        if text.startswith(("/", "!")):
            # A command is not something to say to the model. submit() owns the
            # dispatch for both prefixes and runs them whether or not a turn is
            # in flight, so steering one would only send its text as prose and
            # leave the command itself unrun.
            self.run_worker(self.submit(text), name="submit", exclusive=False)
            return

        if not self.is_busy:
            # Nothing to steer into: this is simply the next thing said.
            self.run_worker(self.submit(text), name="submit", exclusive=False)
            return

        self._transcript.add_user_message(text)
        if self.loop.steer(text):
            self._transcript.add_notice("steering - the model call was cut short", "info")
        else:
            self._transcript.add_notice("steering - lands after the running tools", "info")

    @property
    def queued(self) -> list[str]:
        """Messages waiting for the running turn to end, oldest first."""
        return list(self._queued)

    def clear_queue(self) -> int:
        """Drop everything queued, returning how much was dropped."""
        count = len(self._queued)
        self._queued.clear()
        # Their echo suppression goes with them; left behind, it would swallow
        # the next identical message the user types.
        self._already_echoed.clear()
        self._sync_queue_depth()
        return count

    def steer_queued(self, index: int) -> None:
        """Promote one queued message into the running turn."""
        if not 0 <= index < len(self._queued):
            return
        self.steer(self._queued.pop(index))
        self._sync_queue_depth()

    def _sync_queue_depth(self) -> None:
        self._status.set_queued(len(self._queued))

    async def _run_command(self, text: str) -> None:
        """Run one slash command, reporting failures instead of tearing the app down.

        A command that raises would otherwise take the whole session with it.
        """
        try:
            await self.commands.dispatch(self._command_context(), text)
        except Exception as exc:  # a bad command must not kill the app
            self.notice(f"{text.split(' ')[0]} failed: {exc!r}", "error")

    async def submit_to_model(self, text: str) -> None:
        """Send a prompt to the model without echoing it as user input.

        Used by commands like /init that expand into a prompt of their own.
        """
        self._transcript.add_notice("Running /init…", "info")
        self._turn_worker = self.run_worker(self._run_turn(text), name="turn", exclusive=False)

    async def run_shell_passthrough(self, command: str) -> None:
        """``!command`` - run a shell command directly, without a model turn.

        It goes through the same permission engine and sandbox as a model-issued
        command; a shortcut that skipped those would be a hole in both.
        """
        if not command:
            return
        self._transcript.add_user_message(f"!{command}")
        self.run_worker(self._run_shell(command), name="shell", exclusive=False)

    async def _run_shell(self, command: str) -> None:
        from hx.tools.base import ToolContext

        tool_use_id = f"shell_{id(command):x}"
        if not self.tools.has("Bash"):
            self._transcript.add_notice("No shell is attached to this session.", "error")
            return

        if not await self._permit_shell(command):
            self._transcript.add_notice(f"Refused: !{command}", "warning")
            return

        self._transcript.add_tool_block(tool_use_id, "Bash", {"command": command})
        ctx = ToolContext(
            cwd=self.settings.cwd,
            session_id=self.loop.session.meta.session_id,
            tool_use_id=tool_use_id,
            settings=self.settings,
            emit_progress=lambda chunk: self._transcript.update_tool_block(tool_use_id, chunk),
        )
        result = await self.tools.call("Bash", {"command": command}, ctx)
        self._transcript.finish_tool_block(
            tool_use_id,
            result.summary,
            result.is_error,
            detail=result.content if result.is_error else "",
        )

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
            )
        )
        return allowed

    async def _run_turn(self, text: str) -> None:
        from hx.config import EnterWhileBusy

        prompt_input = self._prompt
        prompt_input.set_running(
            True,
            enter_steers=self.settings.tui.enter_while_busy is EnterWhileBusy.STEER,
        )
        interrupted = False
        try:
            await self.loop.run(text)
        except asyncio.CancelledError:
            interrupted = True
            self._transcript.add_notice("interrupted", "warning")
        finally:
            prompt_input.set_running(False)
            self._working.stop()

        # A steer that never got delivered - an interrupt landed first - is
        # still something the user said, so it goes back in the queue rather
        # than disappearing with the turn. It is already on screen from when it
        # was steered, so the replay through submit() must not echo it again.
        returned = self.loop.take_pending_steer()
        self._already_echoed.extend(returned)
        self._queued[:0] = returned
        self._sync_queue_depth()

        if interrupted:
            # An interrupt stops the agent, the queue included. Starting the
            # next message here would mean the key the user pressed to stop
            # everything launched another turn - most visibly for a steer, which
            # is what they typed just before deciding to stop.
            if self._queued:
                self._transcript.add_notice(
                    f"{len(self._queued)} message(s) still queued · /queue to see them",
                    "info",
                )
            return

        if self._queued:
            # This coroutine is still the active Textual worker until it
            # returns, so submit() would otherwise see a running turn and put
            # the same prompt straight back on the queue.
            self._turn_worker = None
            await self.submit(self._queued.pop(0))
            self._sync_queue_depth()

    def _command_context(self) -> CommandContext:
        return CommandContext(
            app=self,
            settings=self.settings,
            session=self.loop.session,
            registry=self.commands,
        )

    def apply_theme(self, name: str) -> str:
        """Point both halves of the app at one palette.

        Widget chrome resolves through Textual's design tokens and Rich
        renderables resolve through THEME, so both have to be switched or a
        theme change would move only half the screen.
        """
        palette = THEME.use(name)
        theme = textual_theme(palette.name)
        self.register_theme(theme)
        self.theme = theme.name
        if self.is_running:
            self.refresh(layout=True)
        return palette.name

    @property
    def tools(self) -> Any:
        return self.loop.tools

    @property
    def sandbox_backend(self) -> str:
        return self._sandbox_backend

    def set_mode(self, mode: Any) -> None:
        """Change the permission mode from a command or a keybinding."""
        self.mode = mode
        if self.loop.permissions is not None:
            self.loop.permissions.set_mode(mode)
        self._status.set_mode(mode.value, self.sandbox_active, self._sandbox_backend)

    async def action_commands(self) -> None:
        """Open HX's own command palette.

        Textual ships a built-in palette on the same key; it is disabled above
        so ctrl+p reaches the slash commands the user actually has.
        """
        # push_screen_wait needs worker context, and a binding action does not
        # run in one.
        self.run_worker(self._open_command_palette(), name="palette", exclusive=True)

    async def _open_command_palette(self) -> None:
        from hx.tui.widgets.palette import CommandPalette

        chosen = await self.push_screen_wait(CommandPalette(self.commands.all()))
        if chosen:
            await self.submit(f"/{chosen}")

    def set_api_key(self, key: str) -> None:
        """Apply a new credential to the running provider.

        Saved keys are picked up on the next start; this is what makes the
        change take effect now, without losing the session.
        """
        provider = getattr(self.loop, "provider", None)
        setter = getattr(provider, "set_api_key", None)
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
        try:
            task_tool = self.loop.tools.get("Task")
        except Exception:
            return
        runner = getattr(task_tool, "runner", None)
        if runner is not None:
            runner.provider = provider

    def last_message_text(self) -> str | None:
        """Text of the most recent assistant message, for ``/copy``."""
        return self._transcript.cursored_text()

    def notice(self, text: str, level: str = "info") -> None:
        self._transcript.add_notice(text, level)

    def query_one_status(self) -> StatusBar:
        return self._status

    @property
    def last_context(self) -> Any:
        return self.loop.last_context

    @property
    def is_busy(self) -> bool:
        """A turn is in flight. Commands that rewrite the session refuse while
        it is, rather than pulling the transcript out from under it."""
        return self._turn_worker is not None and not self._turn_worker.is_finished

    def rename_outgoing_session(self, session: Any) -> None:
        """Name the session being left behind for what it ended up being.

        Detached: the user asked for a new session, not to wait on a name for
        the old one. It lands in that session's ``meta.json`` when it arrives,
        which is all ``/resume`` reads.
        """
        if not session.messages:
            return
        self.run_worker(
            self.loop.retitle_session(session),
            name="retitle",
            exclusive=False,
        )

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
        self._transcript.clear_all()
        self._transcript.add_notice("New session started.", "success")
        status = self._status
        status.set_tokens(0, 0)
        status.set_cache(0, 0, 0.0)
        status.set_cost(0.0)
        status.set_context(0, self.loop.model_info.context_window if self.loop.model_info else 0)

    def resume_session(self, session_id: str) -> None:
        from hx.core.session import load_session

        try:
            session = load_session(session_id)
        except Exception as exc:
            self.notice(f"Could not resume {session_id}: {exc}", "error")
            return

        self.rename_outgoing_session(self.loop.session)
        self.loop.session = session
        setter = getattr(self.loop.provider, "set_session_id", None)
        if setter is not None:
            setter(session.meta.session_id)
        self._attach_checkpoints()
        self._replay_transcript()
        self._transcript.add_notice(f"Resumed {session_id}.", "success")

    def rewind_to(self, index: int) -> Any:
        """Take the session back to just before message ``index``.

        Files first, then the transcript: if the restore fails the session
        still describes the tree as it actually is. The prompt that was cut is
        handed back to the input box rather than dropped - a rewind is nearly
        always the first half of "say that differently".

        Returns the :class:`~hx.core.checkpoints.RestoreReport`, or ``None``
        when this app has no checkpoint store.
        """
        session = self.loop.session
        prompt = session.messages[index].text() if index < len(session.messages) else ""
        report = None
        if self.checkpoints is not None:
            report = self.checkpoints.restore_to(index, self.tracker)
        session.rewind_to(index)
        self._replay_transcript()
        if prompt:
            self._prompt.text = prompt
            self._prompt.move_cursor(self._prompt.document.end)
            self._prompt.focus()
        return report

    def _attach_checkpoints(self) -> None:
        """Point the store at whichever session the tools are now writing to."""
        if self.checkpoints is None:
            return
        from hx.paths import session_checkpoints_dir

        session = self.loop.session
        self.checkpoints.attach(session, session_checkpoints_dir(session.meta.session_id))

    def _replay_transcript(self) -> None:
        """Redraw the transcript from the session - the screen no longer matches
        the history, so it is rebuilt rather than patched."""
        transcript = self._transcript
        transcript.clear_all()
        for message in self.loop.session.active_messages():
            if message.role == "user":
                transcript.add_user_message(message.text())
            elif text := message.text():
                transcript.start_assistant_message()
                transcript.append_delta(text)

    async def action_interrupt(self) -> None:
        if not self.is_busy:
            return
        self.loop.cancel()
        self._turn_worker.cancel()

    async def action_selection_copy(self) -> None:
        """Copy what is selected in the transcript, or let the key move on.

        Textual binds this key on the *screen*, which the prompt never lets it
        reach: the prompt is a TextArea, it holds focus for the whole session,
        and it binds the same key to its own copy. So a selection dragged over
        the transcript was highlighted and then copied by nothing at all.

        Raising ``SkipAction`` when there is no transcript selection is what
        keeps the rest of the key's meaning intact: it carries on down the
        chain to ``app.clear``, so ctrl+c on an untouched transcript still
        clears the draft and still exits on the second press.
        """
        from textual.actions import SkipAction

        selected = self.screen.get_selected_text()
        if not selected:
            raise SkipAction()
        self.screen.clear_selection()
        await self.copy(selected)

    async def action_clear(self) -> None:
        """Clear the prompt; on an already-empty prompt, a second press exits.

        pi's behaviour, and the reason ctrl+c does not cancel a turn from the
        prompt: escape does that, and a key that sometimes discards a draft and
        sometimes kills a turn is a key nobody presses confidently.

        With an empty prompt during a running turn, the key means what every
        terminal user expects it to mean. A draft still takes priority so it
        can be cleared without interrupting the agent; escape always interrupts.
        """
        if self._prompt.text:
            self._prompt.clear()
            self._clear_armed = False
            return
        if self.is_busy:
            await self.action_interrupt()
            return
        if self._clear_armed:
            self.exit()
            return
        self._clear_armed = True
        self.notice(f"Press {KEYMAP.text('app.clear')} again to exit.", "info")

    async def action_exit(self) -> None:
        """Exit, but only from an empty prompt - ctrl+d deletes otherwise."""
        if not self._prompt.text:
            self.exit()

    async def action_suspend(self) -> None:
        """Drop to the shell with SIGTSTP, the way any other terminal app does."""
        import signal

        with self.suspend():
            os.kill(os.getpid(), signal.SIGTSTP)

    async def action_mode_cycle(self) -> None:
        from hx.config import PermissionMode

        order = list(PermissionMode)
        self.set_mode(order[(order.index(self.mode) + 1) % len(order)])

    async def action_model_select(self) -> None:
        await self.submit("/model")

    async def action_tools_expand(self) -> None:
        """Expand tool output - or the startup header, while it is still up.

        The header says this key shows every shortcut, so it has to, and the
        user has not run a tool yet at the point they read that.
        """
        header = getattr(self, "_header", None)
        if header is not None and not self._transcript._tools:
            header.toggle()
            return
        self._transcript.toggle_expanded()

    def _refresh_branch(self) -> None:
        """Re-read the branch and repaint only when it moved.

        Unconditional repainting would mark the status bar dirty once a second
        for the life of the session; the watcher already knows when there is
        nothing to say.
        """
        previous = self._branch.branch
        current = self._branch.poll()
        if current != previous or self._status.branch != current:
            self._status.set_location(_home_relative(self.settings.cwd), current)

    def _sync_frame(self) -> None:
        """Keep the prompt's rules in step with focus and the draft's height."""
        focused = self._prompt.has_focus
        self._working.set_focused_style(focused)
        self._bottom_rule.set_focused_style(focused)

        hidden_above = self._prompt.scroll_offset.y
        visible = self._prompt.size.height
        hidden_below = max(0, self._prompt.document.line_count - hidden_above - visible)
        self._working.set_hidden_above(hidden_above)
        self._bottom_rule.set_hidden_below(hidden_below)

    def on_text_area_changed(self, event: Any) -> None:
        self._sync_frame()

    def on_descendant_focus(self, event: Any) -> None:
        self._sync_frame()

    def on_descendant_blur(self, event: Any) -> None:
        self._sync_frame()

    async def action_todos_toggle(self) -> None:
        self._todos.set_visible("visible" not in self._todos.classes)

    async def action_message_copy(self) -> None:
        """Copy the cursored message, or the last answer when none is cursored."""
        text = self._transcript.cursored_text()
        if not text:
            self.notice("Nothing to copy yet.", "warning")
            return
        await self.copy(text)

    def set_mouse_reporting(self, enabled: bool) -> bool:
        """Turn the terminal's mouse reporting on or off. Returns whether it took.

        With reporting on, the terminal hands drags to HX and its own
        click-and-drag selection is unavailable - which is why a selection that
        HX cannot extract text from looks like a terminal that has stopped
        letting you copy. Turning it off gives the terminal back its native
        selection, at the cost of scroll-wheel and click inside HX.

        Textual has no public switch for this, so the driver's own enable and
        disable are used. They write four escape sequences each and keep no
        state beyond that, so toggling mid-session is safe; a driver without
        them (the headless one in tests) reports failure rather than raising.
        """
        driver = self._driver
        method = getattr(
            driver,
            "_enable_mouse_support" if enabled else "_disable_mouse_support",
            None,
        )
        if driver is None or method is None:
            return False
        method()
        self.mouse_reporting = enabled
        return True

    def copy_to_clipboard(self, text: str) -> None:
        """Textual's clipboard entry point, redirected through HX's.

        Textual's own implementation is OSC 52 and nothing else, which macOS
        Terminal ignores outright and iTerm2 ships disabled. Every copy that
        went through it - notably ctrl+c on a mouse selection, which Textual
        binds on the screen - therefore did nothing at all, silently, on the
        two terminals most HX users are running.

        Overriding here rather than rebinding the key catches Textual's
        internal callers too, so there is one clipboard path in the app and one
        notice saying which mechanism took the text.
        """
        self.run_worker(self.copy(text), name="clipboard", exclusive=False)

    async def copy(self, text: str) -> None:
        """Copy to the system clipboard and say so, or say why not."""
        from hx.tui.clipboard import ClipboardError, copy_text, format_size

        try:
            # The base implementation, not ``self.copy_to_clipboard`` - which is
            # now this method's caller, and would loop.
            via = await copy_text(text, write_osc52=super().copy_to_clipboard)
        except ClipboardError as exc:
            self.notice(f"Could not copy: {exc}", "error")
            return
        self.notice(f"Copied {format_size(text)} to the clipboard ({via}).", "success")

    async def action_transcript_page_up(self) -> None:
        self._transcript.page_up()

    async def action_transcript_page_down(self) -> None:
        self._transcript.page_down()

    async def action_transcript_top(self) -> None:
        self._transcript.scroll_to_top()

    async def action_transcript_bottom(self) -> None:
        self._transcript.scroll_to_bottom()

    async def action_transcript_previous_prompt(self) -> None:
        self._transcript.move_cursor(-1)

    async def action_transcript_next_prompt(self) -> None:
        self._transcript.move_cursor(1)

    def dismiss_modal(self, screen: Any) -> None:
        """Tear a modal down whatever state it reached.

        A flow that opens a modal must be able to guarantee it closes again,
        including when the flow itself blew up: an orphaned modal owns the
        keyboard and answers to nothing, which costs the user the session.
        ``dismiss`` only works on the active screen, so anything else is left
        to whoever is on top of it.
        """
        try:
            if screen in self.screen_stack and self.screen is screen:
                screen.dismiss(None)
        except Exception:  # a failed teardown must not mask the real error
            pass

    async def ask_permission(self, request: Any) -> Any:
        """Ask in the transcript and wait for the answer.

        Inline rather than in a modal: the model has just said what it intends
        to do, and covering that sentence with a dialog at the moment the user
        has to judge it is the wrong trade. Answering leaves the block behind as
        a permanent record of what was granted.

        The turn is blocked here, so the working indicator has to stop claiming
        the tool is running - it is waiting on a person.
        """
        from hx.permissions.engine import PermissionAnswer
        from hx.tui.widgets.permission import PermissionPrompt

        future: asyncio.Future[PermissionAnswer] = asyncio.get_running_loop().create_future()
        prompt = PermissionPrompt(request, future, origin=getattr(request, "origin", None))
        self._transcript.add_permission_prompt(prompt)
        self._focus_pending_prompt()

        # Restored rather than stopped: the turn is still running, and the next
        # thing the user sees should be the tool they just approved carrying on,
        # not the indicator claiming the whole turn is waiting on them.
        resumed = self._working.label
        self._working.start("awaiting approval")
        try:
            return await future
        except asyncio.CancelledError:
            # An interrupt landed while this was still open. Nobody answered it,
            # and a block that goes on offering keys that reach nothing is worse
            # than one that says so.
            prompt.abandon()
            raise
        finally:
            if self._working.busy:
                self._working.start(resumed)
            self._focus_pending_prompt()

    def _focus_pending_prompt(self) -> None:
        """Hand the keyboard to the oldest unanswered prompt, or back to the composer.

        A queue rather than a single widget, because concurrent subagents can
        each be stopped at an approval. Answering the top one passes the keys to
        the next; returning them to the composer in between would feed the next
        prompt's ``y`` into a half-typed message instead.
        """
        pending = self._transcript.pending_permission_prompts()
        (pending[0] if pending else self._prompt).focus()


def _home_relative(path: Path) -> str:
    """``~/src/hx`` - the way a user says where they are."""
    try:
        return f"~/{path.resolve().relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _default_auth() -> Any:
    """Stand-alone apps (tests, `textual run`) still need a resolver."""
    from hx.auth.resolve import AuthResolver

    return AuthResolver()


async def run_tui(
    loop: AgentLoop,
    bus: EventBus,
    settings: Settings,
    models: ModelRegistry | None = None,
    auth: Any = None,
    sandbox_active: bool = True,
    sandbox_backend: str = "none",
    skills: Any = None,
    agents: Any = None,
    mcp: Any = None,
    notices: list[str] | None = None,
    checkpoints: Any = None,
    tracker: Any = None,
) -> None:
    app = HXApp(
        loop,
        bus,
        settings,
        models=models,
        auth=auth,
        sandbox_active=sandbox_active,
        sandbox_backend=sandbox_backend,
        skills=skills,
        agents=agents,
        mcp=mcp,
        notices=notices,
        checkpoints=checkpoints,
        tracker=tracker,
    )
    await app.run_async()
