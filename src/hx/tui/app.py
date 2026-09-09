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
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal

from hx.core import events as ev
from hx.core.context import git_branch
from hx.core.usage import format_tokens
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


class HXApp(App[None]):
    """Top-level Textual app."""

    CSS_PATH = "hx.tcss"
    ENABLE_COMMAND_PALETTE = False
    """Textual's built-in palette would shadow ctrl+p; HX has its own."""
    #: Built from the keybinding registry, so the keys here, the ones ``/help``
    #: prints and the ones the hints bar shows cannot drift apart.
    BINDINGS: ClassVar[list[BindingType]] = bindings_for(  # type: ignore[assignment]
        "app.interrupt",
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
        api_key: str = "",
        sandbox_active: bool = True,
        sandbox_backend: str = "none",
        skills: Any = None,
        agents: Any = None,
        mcp: Any = None,
        notices: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._startup_notices = list(notices or ())
        self.sandbox_active = sandbox_active
        self._sandbox_backend = sandbox_backend
        self.skills = skills or {}
        self.agents = agents or {}
        self.mcp = mcp
        self.loop = loop
        self.models = models if models is not None else ModelRegistry()
        self.api_key = api_key
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
        self._turn_worker: Any = None
        self._queued: list[str] = []
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
        would fail while a permission modal is up - killing whichever worker
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
        status.set_model(self.loop.model)
        status.set_mode(self.mode.value, self.sandbox_active, self._sandbox_backend)
        status.set_location(_home_relative(self.settings.cwd), git_branch(self.settings.cwd))
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
        await self.submit(message.text)

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

        if self._turn_worker is not None and not self._turn_worker.is_finished:
            # Do not interleave turns: queue and run it when the current one ends.
            self._queued.append(text)
            self._transcript.add_notice("queued", "info")
            return

        self._transcript.add_user_message(text)
        # A Textual worker, not a bare task: the permission modal uses
        # push_screen_wait, which is only valid inside worker context.
        self._turn_worker = self.run_worker(self._run_turn(text), name="turn", exclusive=False)

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
        self._transcript.finish_tool_block(tool_use_id, result.summary, result.is_error)

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
        prompt_input = self._prompt
        prompt_input.set_running(True)
        try:
            await self.loop.run(text)
        except asyncio.CancelledError:
            self._transcript.add_notice("interrupted", "warning")
        finally:
            prompt_input.set_running(False)
            self._working.stop()

        if self._queued:
            # This coroutine is still the active Textual worker until it
            # returns, so submit() would otherwise see a running turn and put
            # the same prompt straight back on the queue.
            self._turn_worker = None
            await self.submit(self._queued.pop(0))

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
        self.api_key = key
        provider = getattr(self.loop, "provider", None)
        setter = getattr(provider, "set_api_key", None)
        if setter is not None:
            setter(key)

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

    def start_new_session(self) -> None:
        """Fresh transcript, same directory. The old session stays on disk."""
        from hx.core.session import new_session

        self.loop.session = new_session(self.settings.cwd, self.loop.model)
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

        self.loop.session = session
        transcript = self._transcript
        transcript.clear_all()
        for message in session.active_messages():
            if message.role == "user":
                transcript.add_user_message(message.text())
            elif text := message.text():
                transcript.start_assistant_message()
                transcript.append_delta(text)
        transcript.add_notice(f"Resumed {session_id}.", "success")

    async def action_interrupt(self) -> None:
        if self._turn_worker is None or self._turn_worker.is_finished:
            return
        self.loop.cancel()
        self._turn_worker.cancel()

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
        if self._turn_worker is not None and not self._turn_worker.is_finished:
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

    async def copy(self, text: str) -> None:
        """Copy to the system clipboard and say so, or say why not."""
        from hx.tui.clipboard import ClipboardError, copy_text, format_size

        try:
            via = await copy_text(text, write_osc52=self.copy_to_clipboard)
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

    async def ask_permission(self, request: Any) -> Any:
        """Show the approval modal and return the user's choice."""
        from hx.permissions.engine import PermissionAnswer
        from hx.tui.widgets.permission import PermissionModal

        answer = await self.push_screen_wait(
            PermissionModal(request, origin=getattr(request, "origin", None))
        )
        return answer if answer is not None else PermissionAnswer(allowed=False)


def _home_relative(path: Path) -> str:
    """``~/src/hx`` - the way a user says where they are."""
    try:
        return f"~/{path.resolve().relative_to(Path.home())}"
    except ValueError:
        return str(path)


async def run_tui(
    loop: AgentLoop,
    bus: EventBus,
    settings: Settings,
    models: ModelRegistry | None = None,
    api_key: str = "",
    sandbox_active: bool = True,
    sandbox_backend: str = "none",
    skills: Any = None,
    agents: Any = None,
    mcp: Any = None,
    notices: list[str] | None = None,
) -> None:
    app = HXApp(
        loop,
        bus,
        settings,
        models=models,
        api_key=api_key,
        sandbox_active=sandbox_active,
        sandbox_backend=sandbox_backend,
        skills=skills,
        agents=agents,
        mcp=mcp,
        notices=notices,
    )
    await app.run_async()
