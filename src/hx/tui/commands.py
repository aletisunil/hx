"""Slash commands.

Registered declaratively so the palette, ``/help``, and tab completion all read
from one source rather than drifting apart.

Only implemented commands are registered. A command that exists in the palette
but does nothing is worse than one that is not there yet.
"""

from __future__ import annotations

import asyncio
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
    aliases: tuple[str, ...] = ()
    """Other spellings users reach for. They resolve, but only ``name`` is listed."""


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._aliases: dict[str, str] = {}

    def register(self, command: Command) -> None:
        self._commands[command.name] = command
        for alias in command.aliases:
            self._aliases[alias] = command.name

    def get(self, name: str) -> Command:
        try:
            return self._commands[self._aliases.get(name, name)]
        except KeyError:
            raise UnknownCommand(name) from None

    def all(self) -> list[Command]:
        return [self._commands[name] for name in sorted(self._commands)]

    def complete(self, prefix: str) -> list[Command]:
        needle = prefix.lstrip("/").lower()
        return [
            c
            for c in self.all()
            if c.name.startswith(needle) or any(a.startswith(needle) for a in c.aliases)
        ]

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
    """``/model [query]`` - fuzzy picker over the models this machine can reach,
    showing route, context window, price per Mtok, and cache support."""
    from hx.providers.models import ModelRegistry
    from hx.tui.widgets.palette import ModelPicker

    registry: ModelRegistry = ctx.app.models
    models = _reachable(ctx, registry.all())
    if not models:
        # A recorded failure replaces the remedy rather than trailing it:
        # "run /models refresh" is not advice when refresh is what failed.
        ctx.app.notice(
            f"No models available. {registry.refresh_error}"
            if registry.refresh_error
            else "No models available. Run /models refresh, or /login to sign in.",
            "warning",
        )
        return

    query = args.strip()
    if query:
        matches = registry.search(query)
        # An id typed in full is a choice, not a query: it prefixes its own
        # variants (…-flash, …-flash-0731), so matching alone never narrows it.
        exact = next((m for m in matches if m.id.lower() == query.lower()), None)
        if exact is not None or len(matches) == 1:
            await _switch_model(ctx, exact.id if exact is not None else matches[0].id)
            return
        if not matches:
            # Falling back to the whole catalogue silently looks like the filter
            # is broken; say that nothing matched instead.
            ctx.app.notice(f"No model matches {query!r}. Showing the full catalogue.", "warning")
            query = ""
        else:
            models = matches

    chosen = await ctx.app.push_screen_wait(
        ModelPicker(models, ctx.session.meta.model, initial=query)
    )
    if chosen:
        await _switch_model(ctx, chosen)


def _reachable(ctx: CommandContext, models: list[Any]) -> list[Any]:
    """Only models on a route this machine has a credential for.

    Offering a model that cannot be called turns a picker choice into a failed
    turn several seconds later, with the cause a screen away.
    """
    from hx.providers import registry

    allowed = {spec.id for spec in registry.available(ctx.app.auth)}
    # The route this session is already running on stays listed whatever the
    # credential store says: it is demonstrably working, and hiding the current
    # model from its own picker is never the right answer.
    allowed.add(registry.provider_for(ctx.app.loop.model).id)
    return [model for model in models if model.provider_id in allowed]


async def _switch_model(ctx: CommandContext, model_id: str) -> None:
    """Switching resets the cached prefix - the new model has its own cache.

    Nothing here second-guesses whether the route will accept the model. The
    subscription plan on the credential looked like it would answer that and
    does not - Codex refuses every model on a ``plus`` account exactly as it
    does on a ``free`` one - so a switch that might fail is allowed to fail,
    where the provider's own error can say so.
    """
    from hx.auth.resolve import ExpiredCredential, MissingCredential

    info = ctx.app.models.get_or_default(model_id)
    try:
        await ctx.app.use_route_for(model_id)
    except (MissingCredential, ExpiredCredential) as exc:
        ctx.app.notice(str(exc), "error")
        return
    ctx.app.loop.set_model(model_id, info)
    ctx.app.query_one_status().set_model(model_id, subscription=info.is_subscription)
    # Resolved per model, so the depth can change without the user asking: the
    # bar has to say so at the moment it happens.
    ctx.app.query_one_status().set_effort(ctx.app.models.displayed_effort(model_id))
    ctx.app.query_one_status().set_context(0, info.context_window)
    billing = (
        f"subscription, {info.provider_id}"
        if info.is_subscription
        else f"per token, via {info.provider_id}"
    )
    ctx.app.notice(
        f"Model set to {model_id} ({format_tokens(info.context_window)} context, "
        f"cache: {info.cache_mode}, {billing})",
        "success",
    )
    _persist_model_choice(ctx, model_id)


def _persist_model_choice(ctx: CommandContext, model_id: str) -> None:
    """Record the choice in the user settings file so new sessions reuse it.

    The switch itself has already happened, so a failure here is a warning, not
    an error - the session keeps running on the new model either way.
    """
    import os

    if not _persist_model_setting(ctx, "model", model_id, "the model choice"):
        return

    # A higher layer setting models.model would quietly win next session, so say so
    # rather than letting the user believe the choice stuck.
    if os.environ.get("HX_MODEL"):
        ctx.app.notice("$HX_MODEL overrides this on the next start.", "warning")
        return
    _warn_if_pinned(ctx, "model")


def _persist_model_setting(ctx: CommandContext, key: str, value: Any, label: str) -> bool:
    """Write one ``models.<key>`` to the user settings file.

    ``label`` names the setting the way the user asked for it - "the model
    choice", "the reasoning effort" - because a failure notice naming
    ``models.reasoning_effort`` describes the file rather than what was lost.

    Returns False when it could not be written, having said so: the session has
    already changed either way, and losing the file is not worth losing the turn.
    """
    from hx.config import ConfigError, read_settings_file, write_settings_file
    from hx.paths import user_settings_file

    path = user_settings_file()
    try:
        data = read_settings_file(path)
        models = data.get("models")
        if not isinstance(models, dict):
            models = {}
            data["models"] = models
        if value is None:
            models.pop(key, None)
        else:
            models[key] = value
        write_settings_file(path, data)
    except (ConfigError, OSError) as exc:
        ctx.app.notice(f"Could not save {label} to {path}: {exc}", "warning")
        return False
    return True


def _warn_if_pinned(ctx: CommandContext, key: str) -> None:
    """Say when a project layer will overrule what was just saved.

    Both project layers sit above the user file, so either one pinning the same
    key would quietly win next session.
    """
    from hx.config import ConfigError, read_settings_file
    from hx.paths import project_local_settings_file, project_settings_file

    for path in (
        project_settings_file(ctx.settings.cwd),
        project_local_settings_file(ctx.settings.cwd),
    ):
        try:
            models = read_settings_file(path).get("models")
        except ConfigError:
            continue
        pinned = models.get(key) if isinstance(models, dict) else None
        if pinned:
            ctx.app.notice(
                f"{path} pins models.{key} to {pinned} and overrides this on the next start.",
                "warning",
            )
            return


async def cmd_effort(ctx: CommandContext, args: str) -> None:
    """``/effort [level|default]`` - how hard the model should think.

    Takes effect on the next turn: the provider asks for the depth per request,
    so nothing has to be rebuilt and a turn already running is left alone.
    """
    from hx.providers.codex_catalogue import EFFORT_ORDER
    from hx.tui.widgets.palette import DEFAULT_EFFORT_ROW, EffortPicker

    model_id = ctx.app.loop.model
    info = ctx.app.models.get_or_default(model_id)
    choice = args.strip().lower()

    if not choice:
        if not info.reasoning_levels:
            ctx.app.notice(
                f"{model_id} publishes no reasoning levels, so there is nothing to pick "
                "from. `/models refresh` fetches them for a Codex model.",
                "warning",
            )
            return
        picked = await ctx.app.push_screen_wait(EffortPicker(info, ctx.app.models.requested_effort))
        if picked is None:
            return
        choice = picked

    if choice in (DEFAULT_EFFORT_ROW, "auto"):
        level: str | None = None
    elif choice in EFFORT_ORDER:
        level = choice
    else:
        known = ", ".join((DEFAULT_EFFORT_ROW, *EFFORT_ORDER))
        ctx.app.notice(f"Unknown effort {choice!r}. Try one of: {known}", "warning")
        return

    ctx.app.models.set_reasoning_effort(level)
    effective = ctx.app.models.reasoning_effort(model_id)
    ctx.app.query_one_status().set_effort(ctx.app.models.displayed_effort(model_id))
    _persist_model_setting(ctx, "reasoning_effort", level, "the reasoning effort")
    _warn_if_pinned(ctx, "reasoning_effort")

    name = model_id.split("/")[-1]
    if level is None:
        ctx.app.notice(
            f"Reasoning effort left to each model. {name} runs at {effective or 'its own default'}.",
            "success",
        )
    elif effective != level:
        # Clamped rather than refused, and silence here would read as the
        # deeper setting having taken.
        ctx.app.notice(
            f"Reasoning effort set to {level}. {name} tops out at {effective}, so it runs there.",
            "success",
        )
    else:
        ctx.app.notice(f"Reasoning effort set to {level}.", "success")
    if not info.reasoning_levels and level is not None:
        ctx.app.notice(
            f"{name} does not publish its reasoning levels, so this is sent as asked "
            "and the route decides. Only Codex models use it today.",
            "warning",
        )


async def cmd_models(ctx: CommandContext, args: str) -> None:
    """``/models refresh`` - re-fetch the catalogue."""
    if args.strip() != "refresh":
        ctx.app.notice("Usage: /models refresh", "warning")
        return
    from hx.net import describe

    try:
        await ctx.app.models.refresh(ctx.app.auth)
    except Exception as exc:
        ctx.app.notice(f"Model refresh failed: {describe(exc)}", "error")
        return
    if ctx.app.models.codex_error:
        # Not fatal - the rest of the catalogue refreshed - but silence here
        # reads as "your subscription has these two models", which is a lie.
        ctx.app.notice(
            f"Could not list this account's Codex models: {ctx.app.models.codex_error}",
            "warning",
        )
    ctx.app.notice(f"Refreshed {len(ctx.app.models.all())} models.", "success")


async def cmd_clear(ctx: CommandContext, args: str) -> None:
    """``/clear`` - start a fresh session in the same directory."""
    if ctx.app.is_busy:
        ctx.app.notice("Interrupt the running turn before clearing.", "warning")
        return
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

    if ctx.app.is_busy:
        ctx.app.notice("Interrupt the running turn before resuming another.", "warning")
        return

    sessions = list_sessions(ctx.settings.cwd)
    if not sessions:
        ctx.app.notice("No previous sessions in this directory.", "warning")
        return
    chosen = await ctx.app.push_screen_wait(SessionPicker(sessions))
    if chosen:
        ctx.app.resume_session(chosen)


async def cmd_rewind(ctx: CommandContext, args: str) -> None:
    """``/rewind`` - take the session back to an earlier prompt, files and all."""
    from hx.tui.widgets.palette import RewindPicker

    if ctx.app.is_busy:
        ctx.app.notice("Interrupt the running turn before rewinding.", "warning")
        return

    points = ctx.app.loop.session.rewind_points()
    if not points:
        ctx.app.notice("Nothing to rewind to yet.", "warning")
        return

    chosen = await ctx.app.push_screen_wait(RewindPicker(points))
    if chosen is None:
        return

    report = ctx.app.rewind_to(int(chosen))
    if report is None:
        ctx.app.notice("Rewound. No file checkpoints in this session.", "success")
        return

    ctx.app.notice(f"Rewound. {report.summary()}.", "success")
    for path in report.conflicted:
        ctx.app.notice(f"{path} changed since HX wrote it - left as it is.", "warning")
    for path, reason in report.skipped:
        ctx.app.notice(f"{path} not restored: {reason}.", "warning")


async def cmd_title(ctx: CommandContext, args: str) -> None:
    """``/title [text]`` - show or set the name this session shows in ``/resume``."""
    session = ctx.app.loop.session
    if not args.strip():
        current = session.meta.title
        ctx.app.notice(
            f"Session title: {current}" if current else "This session is not named yet.",
            "info" if current else "warning",
        )
        return
    session.set_title(args.strip())
    ctx.app.notice(f"Session title: {session.meta.title}", "success")


async def cmd_prompt(ctx: CommandContext, args: str) -> None:
    """``/prompt`` - the system prompt this session is actually running with."""
    from hx.core.context import resolve_system_prompt

    in_use = ctx.app.loop.context.system_prompt
    lines = [in_use.rstrip()]

    resolved = resolve_system_prompt(ctx.settings.cwd, ctx.settings.prompt)
    lines.append(f"\nSource: {resolved.source}")
    for append in resolved.appends:
        lines.append(f"Appended: {append}")
    if resolved.text.strip() != in_use.strip():
        # The prompt is read once at startup to keep the cache prefix stable, so
        # an override edited since then is real but not yet in force.
        lines.append("An override has changed on disk since startup; it applies next run.")
    ctx.app.notice("\n".join(lines))


async def cmd_cost(ctx: CommandContext, args: str) -> None:
    """``/cost`` - per-turn token and cost breakdown including cache savings."""
    usage = ctx.session.usage
    meta = ctx.session.meta
    prompts = meta.prompt_count
    # "calls" alone invited the question of why /resume reports a different,
    # larger number: these count provider requests, that counts wire-format
    # message records, and a single prompt produces many of both.
    scale = f"{len(usage.turns)} API requests"
    if prompts:
        scale += f" across {prompts} prompt{'s' if prompts != 1 else ''}"
    lines = [
        f"Session cost:  {format_cost(usage.total_cost_usd)}  over {scale}",
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


async def cmd_permissions(ctx: CommandContext, args: str) -> None:
    """``/permissions`` - view the active rules and what is actually enforcing them."""
    engine = ctx.app.loop.permissions
    if engine is None:
        ctx.app.notice("No permission engine is attached to this session.", "warning")
        return

    lines = [f"Permission mode: {engine.mode.value}"]
    sandbox = ctx.app.sandbox_backend
    lines.append(
        f"Sandbox: {sandbox}"
        if ctx.app.sandbox_active
        else "Sandbox: none - commands are NOT confined by the OS"
    )

    if engine.rules:
        lines.append("")
        lines.append("Rules (deny wins, then ask, then allow):")
        for decision in ("deny", "ask", "allow"):
            for rule in engine.rules:
                if rule.decision.value != decision:
                    continue
                spec = f"({rule.specifier})" if rule.specifier else ""
                lines.append(f"  {decision:<5} {rule.tool}{spec}    [{rule.source}]")
    else:
        lines.append("")
        lines.append('No rules configured. Add them under "permissions" in .hx/settings.json.')

    from hx.paths import project_local_settings_file

    lines.append("")
    lines.append(f'"Always allow" writes to {project_local_settings_file(ctx.settings.cwd)}')
    # Where, and why there: a grant is machine-local, so it is kept per project
    # under the user's home instead of being dropped into the checkout.
    lines.append("  - per project, on this machine only; nothing is written into the repo")
    lines.append("  (this machine only - .hx/settings.json stays yours to check in)")

    ctx.app.notice("\n".join(lines), "info" if ctx.app.sandbox_active else "warning")


async def cmd_mode(ctx: CommandContext, args: str) -> None:
    """``/mode [plan|default|acceptEdits|bypass]``."""
    from hx.config import PermissionMode

    if not args:
        options = ", ".join(mode.value for mode in PermissionMode)
        ctx.app.notice(f"Mode is {ctx.app.mode.value}. Options: {options}")
        return

    wanted = args.strip()
    match = next((m for m in PermissionMode if m.value.lower() == wanted.lower()), None)
    if match is None:
        ctx.app.notice(f"Unknown mode {wanted!r}. Try /mode with no argument.", "error")
        return

    ctx.app.set_mode(match)
    level = "warning" if match is PermissionMode.BYPASS else "success"
    note = " - every tool call is approved automatically" if match is PermissionMode.BYPASS else ""
    ctx.app.notice(f"Permission mode: {match.value}{note}", level)


INIT_PROMPT = """\
Write an AGENTS.md for this project, at its root.

Read enough of the codebase to be accurate. Cover: what the project is, how to
build, test and lint it, the layout of the source tree, and any conventions a
newcomer would otherwise get wrong. Be concise and concrete - it is loaded into
context on every session, so every line costs.

If AGENTS.md already exists, improve it rather than replacing it wholesale."""


async def cmd_init(ctx: CommandContext, args: str) -> None:
    """``/init`` - generate an AGENTS.md describing this project."""
    await ctx.app.submit_to_model(INIT_PROMPT)


async def cmd_configure(ctx: CommandContext, args: str) -> None:
    """``/configure`` - session settings, and set or replace the OpenRouter key."""
    from hx.auth.store import OPENROUTER
    from hx.providers.openrouter import api_key_source, mask_api_key, save_api_key
    from hx.tui.widgets.configure import ConfigureModal, build_summary

    source = api_key_source()
    hint = "not set"
    if source:
        from hx.auth.resolve import ExpiredCredential, MissingCredential

        try:
            key = ctx.app.auth.resolve_static(OPENROUTER).token
            hint = f"{mask_api_key(key)} (from {source})"
        except (MissingCredential, ExpiredCredential):
            hint = f"unusable (from {source})"

    warning = ""
    if source and source.startswith("environment"):
        # Saving to the file while an env var is set would look like a no-op.
        warning = (
            f"{source} takes precedence over the saved key. "
            "Unset it for a new key to take effect in future sessions."
        )

    key = await ctx.app.push_screen_wait(ConfigureModal(build_summary(ctx.app), hint, warning))
    if not key:
        return

    try:
        save_api_key(key)
    except OSError as exc:
        ctx.app.notice(f"Could not save the key: {exc}", "error")
        return

    ctx.app.set_api_key(key)
    # The key itself never reaches the transcript.
    ctx.app.notice(
        f"API key saved ({mask_api_key(key)}) and applied to this session.",
        "success",
    )


async def cmd_login(ctx: CommandContext, args: str) -> None:
    """``/login [provider]`` - sign in to a model route."""
    from hx.providers import registry
    from hx.tui.widgets.login import ProviderPicker, provider_options

    provider_id = args.strip()
    if not provider_id:
        chosen = await ctx.app.push_screen_wait(
            ProviderPicker(provider_options(registry.SPECS, ctx.app.auth))
        )
        if not chosen:
            return
        provider_id = chosen

    try:
        spec = registry.get(provider_id)
    except registry.UnknownProvider:
        known = ", ".join(s.id for s in registry.SPECS)
        ctx.app.notice(f"Unknown provider {provider_id!r}. Known: {known}", "error")
        return

    if not spec.is_subscription:
        # An API key has no flow to run - /configure is already that screen.
        await cmd_configure(ctx, "")
        return

    await _run_oauth_login(ctx, spec)


async def _run_oauth_login(ctx: CommandContext, spec: Any) -> None:
    from hx.auth.oauth import codex as codex_oauth
    from hx.auth.oauth.callback import CallbackError
    from hx.auth.store import AuthStore
    from hx.providers import registry
    from hx.tui.widgets.login import LoginModal

    modal = LoginModal(spec.label)
    # Awaited: the flow talks to the modal immediately, and a screen that has
    # not composed yet has nothing to talk to. Pushing without waiting left the
    # first call raising NoMatches and the modal orphaned on screen - focused,
    # accepting keystrokes, and wired to nothing.
    await ctx.app.push_screen(modal)
    try:
        credential = await codex_oauth.login_browser(modal)
    except asyncio.CancelledError:
        ctx.app.notice("Sign-in cancelled.", "warning")
        return
    except (codex_oauth.OAuthError, CallbackError) as exc:
        ctx.app.notice(f"Sign-in failed: {exc}", "error")
        return
    except Exception as exc:
        # Any other failure is a bug, and a bug must not cost the user their
        # terminal: the modal is torn down in `finally` either way, and the
        # message goes to the transcript where they can read it.
        ctx.app.notice(f"Sign-in failed: {exc}", "error")
        return
    finally:
        ctx.app.dismiss_modal(modal)

    try:
        AuthStore().save(spec.id, credential)
    except OSError as exc:
        ctx.app.notice(f"Signed in, but could not save the credential: {exc}", "error")
        return

    # The tokens themselves never reach the transcript. The plan is named
    # because it is the one thing about the account worth knowing up front -
    # Codex is documented as needing a paid ChatGPT plan - but it is stated as
    # a fact about the login, not as a verdict on whether models will run.
    plan = registry.subscription_plan(spec.id, ctx.app.auth)
    signed_in = f"Signed in to {spec.label}" + (f" ({plan} plan)" if plan else "")
    if plan == codex_oauth.FREE_PLAN:
        ctx.app.notice(
            f"{signed_in}. Codex is documented as requiring a paid ChatGPT plan, "
            "so the models may be refused. Pick one with /model and see.",
            "warning",
        )
        return

    # The model list is per account, so it only means anything once there is an
    # account: ask now rather than leaving /model showing the fallback guess.
    count = await _refresh_after_login(ctx, spec.id)
    offer = f"{count} models available" if count else "Pick a model with /model"
    ctx.app.notice(f"{signed_in}. {offer}.", "success")


async def _refresh_after_login(ctx: CommandContext, provider_id: str) -> int:
    """Re-fetch the catalogue, and report how many models the route now offers.

    A failure here is worth a line but not the sign-in: the credential is
    stored either way, and the fallback list still offers models to try.
    """
    from hx.net import describe

    try:
        await ctx.app.models.refresh(ctx.app.auth)
    except Exception as exc:
        ctx.app.notice(f"Model refresh failed: {describe(exc)}", "warning")
    if ctx.app.models.codex_error:
        ctx.app.notice(
            f"Could not list this account's models: {ctx.app.models.codex_error}", "warning"
        )
    return sum(1 for model in ctx.app.models.all() if model.provider_id == provider_id)


async def cmd_logout(ctx: CommandContext, args: str) -> None:
    """``/logout <provider>`` - forget a stored credential."""
    from hx.auth.store import AuthStore
    from hx.providers import registry

    provider_id = args.strip()
    if not provider_id:
        known = ", ".join(s.id for s in registry.SPECS)
        ctx.app.notice(f"Usage: /logout <provider>. Known: {known}", "warning")
        return

    if AuthStore().delete(provider_id):
        ctx.app.notice(f"Removed the {provider_id} credential.", "success")
    else:
        ctx.app.notice(f"No stored credential for {provider_id}.", "warning")


async def cmd_mouse(ctx: CommandContext, args: str) -> None:
    """``/mouse [on|off]`` - hand drag-selection back to the terminal, or take it back."""
    choice = args.strip().lower()
    if choice not in {"", "on", "off", "toggle"}:
        ctx.app.notice(f"Usage: /mouse [on|off]. Got {args.strip()!r}.", "warning")
        return

    enabled = {"on": True, "off": False}.get(choice, not ctx.app.mouse_reporting)
    if not ctx.app.set_mouse_reporting(enabled):
        ctx.app.notice("This terminal driver has no mouse reporting to toggle.", "warning")
        return

    if enabled:
        ctx.app.notice(
            "Mouse reporting on. Scrolling and clicking work in HX; select with "
            "the mouse and copy with ctrl+c.",
            "success",
        )
    else:
        ctx.app.notice(
            "Mouse reporting off. Your terminal's own selection and copy are back, "
            "and HX no longer sees scroll or clicks. /mouse on restores it. "
            "(In most macOS terminals, holding alt/option while dragging selects "
            "natively without turning this off at all.)",
            "success",
        )


async def cmd_theme(ctx: CommandContext, args: str) -> None:
    """``/theme [dark|light|ansi]`` - switch palette for this session."""
    from hx.tui.theme import THEME, palettes

    available = palettes()
    if not args:
        options = ", ".join(sorted(available))
        ctx.app.notice(f"Theme is {THEME.palette.name}. Options: {options}")
        return

    wanted = args.strip().lower()
    if wanted not in available:
        ctx.app.notice(f"Unknown theme {wanted!r}. Try /theme with no argument.", "error")
        return

    ctx.app.notice(f"Theme: {ctx.app.apply_theme(wanted)}", "success")


async def cmd_help(ctx: CommandContext, args: str) -> None:
    lines = ["Commands:"]
    lines += [
        f"  /{c.name:<12} {c.summary}" + (f" (also /{', /'.join(c.aliases)})" if c.aliases else "")
        for c in ctx.registry.all()
    ]
    lines.append("")
    lines.append("Keys")
    # Generated from the registry rather than typed out: a hand-written key
    # list is a copy that drifts the first time a binding moves.
    lines += [f"  {key:<16} {description}" for key, description in help_keys()]
    lines.append("")
    lines.append("  @path            completes a file")
    lines.append("  !command         runs a shell command directly")
    ctx.app.notice("\n".join(lines))


#: Actions worth listing in ``/help``, in the order a new user meets them.
HELP_ACTIONS = (
    "tui.input.submit",
    "tui.input.newLine",
    "tui.input.complete",
    "app.interrupt",
    "app.clear",
    "app.exit",
    "app.mode.cycle",
    "app.commands",
    "app.model.select",
    "app.tools.expand",
    "app.todos.toggle",
    "app.message.copy",
    "app.transcript.previousPrompt",
    "app.transcript.nextPrompt",
    "app.transcript.top",
    "app.transcript.bottom",
    "app.suspend",
)


def help_keys() -> list[tuple[str, str]]:
    """``(keys, description)`` for every action ``/help`` lists."""
    from hx.keys import KEYMAP

    rows = []
    for action in HELP_ACTIONS:
        keys = KEYMAP.text(action)
        if keys:
            rows.append((keys, KEYMAP.description(action)))
    return rows


async def cmd_copy(ctx: CommandContext, args: str) -> None:
    """``/copy`` - put the last assistant message on the clipboard."""
    text = ctx.app.last_message_text()
    if not text:
        ctx.app.notice("Nothing to copy yet.", "warning")
        return
    await ctx.app.copy(text)


async def cmd_queue(ctx: CommandContext, args: str) -> None:
    """``/queue [steer <n>|clear]`` - see and act on what is waiting."""
    from hx.keys import KEYMAP

    queued = ctx.app.queued
    argument = args.strip()

    if argument in {"clear", "drop"}:
        if not queued:
            ctx.app.notice("Nothing queued.", "warning")
            return
        count = ctx.app.clear_queue()
        ctx.app.notice(f"Dropped {count} queued message(s).", "success")
        return

    if argument.startswith("steer"):
        rest = argument.removeprefix("steer").strip()
        index = 1
        if rest:
            if not rest.isdigit():
                ctx.app.notice("Usage: /queue steer <n>", "warning")
                return
            index = int(rest)
        if not 1 <= index <= len(queued):
            ctx.app.notice(f"No queued message {index}. There are {len(queued)}.", "warning")
            return
        ctx.app.steer_queued(index - 1)
        return

    if argument:
        ctx.app.notice("Usage: /queue [steer <n>|clear]", "warning")
        return

    if not queued:
        ctx.app.notice("Nothing queued.", "info")
        return

    lines = ["Queued, oldest first:"]
    lines += [f"  {position}. {text}" for position, text in enumerate(queued, start=1)]
    lines.append("")
    lines.append(
        f"/queue steer <n> sends one now; {KEYMAP.text('tui.input.steer')} sends the first."
    )
    ctx.app.notice("\n".join(lines), "info")


async def cmd_quit(ctx: CommandContext, args: str) -> None:
    ctx.app.exit()


def build_default_commands() -> CommandRegistry:
    registry = CommandRegistry()
    for command in (
        Command("model", "Choose the model", cmd_model, "[query]", takes_args=True),
        Command("models", "Refresh the model catalogue", cmd_models, "refresh", takes_args=True),
        Command(
            "effort", "Set the reasoning depth", cmd_effort, "[level|default]", takes_args=True
        ),
        Command("clear", "Start a fresh session", cmd_clear),
        Command("compact", "Summarise older turns now", cmd_compact, "[focus]", takes_args=True),
        Command("todos", "Toggle the todo sidebar", cmd_todos),
        Command("resume", "Resume a previous session", cmd_resume),
        Command("rewind", "Go back to an earlier prompt", cmd_rewind),
        Command("title", "Show or set the session name", cmd_title, "[text]", takes_args=True),
        Command("prompt", "Show the system prompt in force", cmd_prompt),
        Command("cost", "Token and cost breakdown", cmd_cost),
        Command("context", "What is filling the context window", cmd_context),
        Command("skills", "List installed skills", cmd_skills),
        Command("agents", "List subagent types", cmd_agents),
        Command("mcp", "MCP server status", cmd_mcp),
        Command("permissions", "Show permission rules and sandbox", cmd_permissions),
        Command("mode", "Set the permission mode", cmd_mode, "[mode]", takes_args=True),
        Command("init", "Generate an AGENTS.md for this project", cmd_init),
        Command("theme", "Switch the colour palette", cmd_theme, "[name]", takes_args=True),
        Command(
            "mouse",
            "Mouse reporting, and with it terminal text selection",
            cmd_mouse,
            "[on|off]",
            takes_args=True,
        ),
        Command("configure", "Settings and the OpenRouter API key", cmd_configure),
        Command("login", "Sign in to a model route", cmd_login, "[provider]", takes_args=True),
        Command("logout", "Forget a stored credential", cmd_logout, "<provider>", takes_args=True),
        Command(
            "queue",
            "Messages waiting for this turn to end",
            cmd_queue,
            "[steer <n>|clear]",
            takes_args=True,
        ),
        Command("copy", "Copy the last reply to the clipboard", cmd_copy),
        Command("help", "List commands and keys", cmd_help),
        Command("quit", "Exit HX", cmd_quit, aliases=("exit", "q")),
    ):
        registry.register(command)
    return registry


class UnknownCommand(Exception):
    pass
