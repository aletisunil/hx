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
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal

from hx.core import events as ev
from hx.core.context import git_branch
from hx.core.usage import format_tokens
from hx.providers.models import ModelRegistry
from hx.tui.commands import CommandContext, CommandRegistry, build_default_commands
from hx.tui.theme import THEME, textual_theme
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
    BINDINGS: ClassVar[list[BindingType]] = [
        ("ctrl+c", "cancel_turn", "Cancel"),
        ("ctrl+d", "quit", "Quit"),
        ("escape", "interrupt", "Interrupt"),
        ("shift+tab", "cycle_mode", "Permission mode"),
        ("ctrl+r", "expand_output", "Expand output"),
        ("ctrl+t", "toggle_todos", "Todos"),
        ("ctrl+p", "hx_commands", "Commands"),
    ]

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
    ) -> None:
        super().__init__()
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
        # Settings are frozen; the live permission mode is session state.
        self.mode = settings.permissions.mode
        self._turn_worker: Any = None
        self._queued: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield Transcript(self.settings.cwd)
            yield TodoSidebar()
        yield SubagentRows()
        yield WorkingIndicator()
        yield PromptInput(self.settings.cwd)
        yield StatusBar()

    async def on_mount(self) -> None:
        """Start the event-bus consumer task and cache the main widgets.

        ``query_one`` resolves against the *active* screen, so every lookup
        would fail while a permission modal is up - killing whichever worker
        made it. The main screen's widgets are therefore looked up once, here,
        and referenced directly from then on.
        """
        self.apply_theme(self.settings.theme)

        self._transcript = self.query_one(Transcript)
        self._status = self.query_one(StatusBar)
        self._subagents = self.query_one(SubagentRows)
        self._todos = self.query_one(TodoSidebar)
        self._prompt = self.query_one(PromptInput)
        self._working = self.query_one(WorkingIndicator)

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

        self._prompt.focus()
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
                    status.set_busy(True, "thinking")
                    working.start("thinking")
                case ev.TextDelta():
                    transcript.append_delta(event.text)
                case ev.ThinkingDelta():
                    transcript.append_thinking(event.text)
                case ev.ToolCallStarted():
                    transcript.add_tool_block(event.tool_use_id, event.name, event.input)
                    status.set_busy(True, event.name)
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
                    status.set_busy(False)
                    working.stop()

    async def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        await self.submit(message.text)

    async def submit(self, text: str) -> None:
        """Handle a user submission: slash command, ``!`` passthrough, or a turn."""
        if text.strip() == "/":
            await self.action_hx_commands()
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
        prompt_input.set_enabled(False)
        try:
            await self.loop.run(text)
        except asyncio.CancelledError:
            self._transcript.add_notice("interrupted", "warning")
        finally:
            prompt_input.set_enabled(True)
            self._status.set_busy(False)
            self._working.stop()

        if self._queued:
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

    async def action_hx_commands(self) -> None:
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

    async def action_cancel_turn(self) -> None:
        await self.action_interrupt()

    async def action_interrupt(self) -> None:
        if self._turn_worker is None or self._turn_worker.is_finished:
            return
        self.loop.cancel()
        self._turn_worker.cancel()

    async def action_cycle_mode(self) -> None:
        from hx.config import PermissionMode

        order = list(PermissionMode)
        self.set_mode(order[(order.index(self.mode) + 1) % len(order)])

    async def action_expand_output(self) -> None:
        self._transcript.toggle_expanded()

    async def action_toggle_todos(self) -> None:
        self._todos.set_visible("visible" not in self._todos.classes)

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
    )
    await app.run_async()
