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
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal

from hx.core import events as ev
from hx.core.context import git_branch
from hx.core.usage import format_tokens
from hx.providers.models import ModelRegistry
from hx.tui.commands import CommandContext, CommandRegistry, build_default_commands
from hx.tui.widgets.input import PromptInput
from hx.tui.widgets.statusbar import StatusBar
from hx.tui.widgets.todos import SubagentRows, TodoSidebar
from hx.tui.widgets.transcript import Transcript

if TYPE_CHECKING:
    from hx.config import Settings
    from hx.core.events import EventBus
    from hx.core.loop import AgentLoop


class HXApp(App[None]):
    """Top-level Textual app."""

    CSS_PATH = "hx.tcss"
    BINDINGS: ClassVar[list[BindingType]] = [
        ("ctrl+c", "cancel_turn", "Cancel"),
        ("ctrl+d", "quit", "Quit"),
        ("escape", "interrupt", "Interrupt"),
        ("shift+tab", "cycle_mode", "Permission mode"),
        ("ctrl+r", "expand_output", "Expand output"),
        ("ctrl+t", "toggle_todos", "Todos"),
    ]

    def __init__(
        self,
        loop: AgentLoop,
        bus: EventBus,
        settings: Settings,
        models: ModelRegistry | None = None,
        api_key: str = "",
        sandbox_active: bool = True,
        skills: Any = None,
        agents: Any = None,
        mcp: Any = None,
    ) -> None:
        super().__init__()
        self.sandbox_active = sandbox_active
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
        # Settings are frozen; the live permission mode is session state.
        self.mode = settings.permissions.mode
        self._turn_worker: Any = None
        self._queued: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield Transcript()
            yield TodoSidebar()
        yield SubagentRows()
        yield PromptInput(self.settings.cwd)
        yield StatusBar()

    async def on_mount(self) -> None:
        """Start the event-bus consumer task and cache the main widgets.

        ``query_one`` resolves against the *active* screen, so every lookup
        would fail while a permission modal is up - killing whichever worker
        made it. The main screen's widgets are therefore looked up once, here,
        and referenced directly from then on.
        """
        self._transcript = self.query_one(Transcript)
        self._status = self.query_one(StatusBar)
        self._subagents = self.query_one(SubagentRows)
        self._todos = self.query_one(TodoSidebar)
        self._prompt = self.query_one(PromptInput)

        status = self._status
        status.set_model(self.loop.model)
        status.set_mode(self.mode.value, self.sandbox_active)
        status.set_location(self.settings.cwd.name, git_branch(self.settings.cwd))
        if self.loop.model_info is not None:
            status.set_context(0, self.loop.model_info.context_window)

        if self.loop.permissions is not None:
            self.loop.permissions.asker = self.ask_permission
            status.set_mode(self.loop.permissions.mode.value, self.sandbox_active)

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

        async for event in self.bus.subscribe():
            match event:
                case ev.TurnStarted():
                    transcript.start_assistant_message()
                    status.set_busy(True, "thinking")
                case ev.TextDelta():
                    transcript.append_delta(event.text)
                case ev.ThinkingDelta():
                    transcript.append_thinking(event.text)
                case ev.ToolCallStarted():
                    transcript.add_tool_block(event.tool_use_id, event.name, event.input)
                    status.set_busy(True, event.name)
                case ev.ToolCallProgress():
                    transcript.update_tool_block(event.tool_use_id, event.chunk)
                case ev.ToolCallFinished():
                    transcript.finish_tool_block(event.tool_use_id, event.summary, event.is_error)
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

    async def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        await self.submit(message.text)

    async def submit(self, text: str) -> None:
        """Handle a user submission: slash command, ``!`` passthrough, or a turn."""
        if text.startswith("/"):
            handled = await self.commands.dispatch(self._command_context(), text)
            if handled:
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

        if self._queued:
            await self.submit(self._queued.pop(0))

    def _command_context(self) -> CommandContext:
        return CommandContext(
            app=self,
            settings=self.settings,
            session=self.loop.session,
            registry=self.commands,
        )

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
        self._transcript.remove_children()
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
        transcript.remove_children()
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
        self.mode = order[(order.index(self.mode) + 1) % len(order)]
        if self.loop.permissions is not None:
            self.loop.permissions.set_mode(self.mode)
        self._status.set_mode(self.mode.value, self.sandbox_active)

    async def action_expand_output(self) -> None:
        self._transcript.toggle_last_tool()

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


async def run_tui(
    loop: AgentLoop,
    bus: EventBus,
    settings: Settings,
    models: ModelRegistry | None = None,
    api_key: str = "",
    sandbox_active: bool = True,
) -> None:
    app = HXApp(loop, bus, settings, models=models, api_key=api_key, sandbox_active=sandbox_active)
    await app.run_async()
