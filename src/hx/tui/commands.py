"""Slash commands.

Registered declaratively so the palette, ``/help``, and tab completion all read
from one source rather than drifting apart.

Only implemented commands are registered. A command that exists in the palette
but does nothing is worse than one that is not there yet.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from hx.core.usage import format_cost, format_tokens

Handler = Callable[["CommandContext", str], Awaitable[None]]


@dataclass(slots=True)
class CommandContext:
    app: Any
    settings: Any
    session: Any
    registry: Any


@dataclass(slots=True)
class Command:
    name: str
    summary: str
    handler: Handler
    args_hint: str = ""
    takes_args: bool = False


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, command: Command) -> None:
        self._commands[command.name] = command

    def get(self, name: str) -> Command:
        try:
            return self._commands[name]
        except KeyError:
            raise UnknownCommand(name) from None

    def all(self) -> list[Command]:
        return [self._commands[name] for name in sorted(self._commands)]

    def complete(self, prefix: str) -> list[Command]:
        needle = prefix.lstrip("/").lower()
        return [c for c in self.all() if c.name.startswith(needle)]

    async def dispatch(self, ctx: CommandContext, line: str) -> bool:
        """Run a ``/command``. Returns False when the line is not a command and
        should be sent to the model instead."""
        if not line.startswith("/"):
            return False

        name, _, args = line[1:].partition(" ")
        try:
            command = self.get(name.strip())
        except UnknownCommand:
            matches = self.complete(name)
            hint = f" Did you mean /{matches[0].name}?" if matches else ""
            ctx.app.notice(f"Unknown command /{name}.{hint} Try /help.", "error")
            return True

        await command.handler(ctx, args.strip())
        return True


# --- handlers -------------------------------------------------------------
# Each opens a modal or mutates session state; none of them talk to the model
# except /compact and /init.


async def cmd_model(ctx: CommandContext, args: str) -> None:
    """``/model [query]`` - fuzzy picker over the OpenRouter catalogue showing
    context window, price per Mtok, and cache support."""
    from hx.providers.models import ModelRegistry
    from hx.tui.widgets.palette import ModelPicker

    registry: ModelRegistry = ctx.app.models
    models = registry.all()
    if not models:
        ctx.app.notice("No model catalogue cached yet. Run /models refresh.", "warning")
        return

    if args:
        matches = registry.search(args)
        if len(matches) == 1:
            _switch_model(ctx, matches[0].id)
            return
        models = matches or models

    chosen = await ctx.app.push_screen_wait(ModelPicker(models, ctx.session.meta.model))
    if chosen:
        _switch_model(ctx, chosen)


def _switch_model(ctx: CommandContext, model_id: str) -> None:
    """Switching resets the cached prefix - the new model has its own cache."""
    info = ctx.app.models.get_or_default(model_id)
    ctx.app.loop.set_model(model_id, info)
    ctx.app.query_one_status().set_model(model_id)
    ctx.app.query_one_status().set_context(0, info.context_window)
    ctx.app.notice(
        f"Model set to {model_id} ({format_tokens(info.context_window)} context, "
        f"cache: {info.cache_mode})",
        "success",
    )


async def cmd_models(ctx: CommandContext, args: str) -> None:
    """``/models refresh`` - re-fetch the catalogue."""
    if args.strip() != "refresh":
        ctx.app.notice("Usage: /models refresh", "warning")
        return
    try:
        await ctx.app.models.refresh(ctx.app.api_key)
    except Exception as exc:
        ctx.app.notice(f"Model refresh failed: {exc}", "error")
        return
    ctx.app.notice(f"Refreshed {len(ctx.app.models.all())} models.", "success")


async def cmd_clear(ctx: CommandContext, args: str) -> None:
    """``/clear`` - start a fresh session in the same directory."""
    ctx.app.start_new_session()


async def cmd_compact(ctx: CommandContext, args: str) -> None:
    """``/compact [instructions]`` - compact now, optionally steering the summary."""
    # The loop publishes CompactionStarted/Finished itself; announcing it here
    # too would race those events and print the step twice.
    compacted = await ctx.app.loop.compact(instructions=args or None, reason="/compact")
    if not compacted:
        ctx.app.notice("Nothing to compact yet.", "warning")


async def cmd_todos(ctx: CommandContext, args: str) -> None:
    """``/todos`` - show or hide the todo sidebar."""
    await ctx.app.action_toggle_todos()


async def cmd_resume(ctx: CommandContext, args: str) -> None:
    """``/resume`` - pick a previous session in this directory."""
    from hx.core.session import list_sessions
    from hx.tui.widgets.palette import SessionPicker

    sessions = list_sessions(ctx.settings.cwd)
    if not sessions:
        ctx.app.notice("No previous sessions in this directory.", "warning")
        return
    chosen = await ctx.app.push_screen_wait(SessionPicker(sessions))
    if chosen:
        ctx.app.resume_session(chosen)


async def cmd_cost(ctx: CommandContext, args: str) -> None:
    """``/cost`` - per-turn token and cost breakdown including cache savings."""
    usage = ctx.session.usage
    lines = [
        f"Session cost:  {format_cost(usage.total_cost_usd)}  over {len(usage.turns)} calls",
        f"Input:         {format_tokens(usage.total_input)} uncached",
        f"Cache read:    {format_tokens(usage.total_cache_read)} "
        f"({usage.cache_hit_rate * 100:.0f}% of prompt tokens)",
        f"Cache write:   {format_tokens(usage.total_cache_write)}",
        f"Output:        {format_tokens(usage.total_output)}",
    ]
    ctx.app.notice("\n".join(lines))


async def cmd_context(ctx: CommandContext, args: str) -> None:
    """``/context`` - what is occupying the window: system, tools, skills index,
    project context, history, injections."""
    assembled = ctx.app.last_context
    if assembled is None:
        ctx.app.notice("No context assembled yet - send a message first.", "warning")
        return
    lines = [f"Context: {format_tokens(assembled.total_tokens)} tokens"]
    lines += [
        f"  {section.name:<12} {format_tokens(section.tokens):>8}"
        for section in assembled.sections
        if section.tokens
    ]
    lines.append(f"  breakpoints  {list(assembled.breakpoints)}")
    ctx.app.notice("\n".join(lines))


async def cmd_skills(ctx: CommandContext, args: str) -> None:
    """``/skills`` - installed skills and which are loaded."""
    skills = ctx.app.skills or {}
    if not skills:
        ctx.app.notice("No skills installed. Add one at .hx/skills/<name>/SKILL.md", "warning")
        return
    lines = [f"Skills ({len(skills)} installed, listed by name and description only):"]
    lines += [
        f"  {skill.name:<20} {skill.description}"
        for skill in sorted(skills.values(), key=lambda s: s.name)
    ]
    ctx.app.notice("\n".join(lines))


async def cmd_agents(ctx: CommandContext, args: str) -> None:
    """``/agents`` - subagent types available to the Task tool."""
    agents = ctx.app.agents or {}
    lines = [f"Agents ({len(agents)}):"]
    for agent in sorted(agents.values(), key=lambda a: a.name):
        tools = ", ".join(agent.tools) if agent.tools else "all tools except Task"
        lines.append(f"  {agent.name:<12} {agent.description}")
        lines.append(f"  {'':<12} tools: {tools}")
    ctx.app.notice("\n".join(lines))


async def cmd_mcp(ctx: CommandContext, args: str) -> None:
    """``/mcp`` - server status and their tools."""
    manager = ctx.app.mcp
    if manager is None or not manager.configs:
        ctx.app.notice("No MCP servers configured. Add one with `hx mcp add`.", "warning")
        return

    lines = ["MCP servers:"]
    for status in manager.status():
        if status.connected:
            lines.append(f"  {status.name:<20} connected, {status.tool_count} tools")
        else:
            lines.append(f"  {status.name:<20} unavailable - {status.error}")
    ctx.app.notice(
        "\n".join(lines), "info" if all(s.connected for s in manager.status()) else "warning"
    )


async def cmd_help(ctx: CommandContext, args: str) -> None:
    lines = ["Commands:"]
    lines += [f"  /{c.name:<12} {c.summary}" for c in ctx.registry.all()]
    lines.append("")
    lines.append("Keys: enter send · ctrl+j newline · esc interrupt · shift+tab mode")
    lines.append("      ctrl+r expand last tool output · ctrl+t todos · ctrl+d quit")
    ctx.app.notice("\n".join(lines))


async def cmd_quit(ctx: CommandContext, args: str) -> None:
    ctx.app.exit()


def build_default_commands() -> CommandRegistry:
    registry = CommandRegistry()
    for command in (
        Command("model", "Choose the model", cmd_model, "[query]", takes_args=True),
        Command("models", "Refresh the model catalogue", cmd_models, "refresh", takes_args=True),
        Command("clear", "Start a fresh session", cmd_clear),
        Command("compact", "Summarise older turns now", cmd_compact, "[focus]", takes_args=True),
        Command("todos", "Toggle the todo sidebar", cmd_todos),
        Command("resume", "Resume a previous session", cmd_resume),
        Command("cost", "Token and cost breakdown", cmd_cost),
        Command("context", "What is filling the context window", cmd_context),
        Command("skills", "List installed skills", cmd_skills),
        Command("agents", "List subagent types", cmd_agents),
        Command("mcp", "MCP server status", cmd_mcp),
        Command("help", "List commands and keys", cmd_help),
        Command("quit", "Exit HX", cmd_quit),
    ):
        registry.register(command)
    return registry


class UnknownCommand(Exception):
    pass
