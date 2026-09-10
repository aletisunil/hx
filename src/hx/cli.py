"""Command-line entry point.

``hx``                 launch the TUI in the current directory
``hx -p "..."``        print mode: run one prompt headless, stream to stdout
``hx resume [id]``     resume a session
``hx prompt``          print the resolved system prompt
``hx mcp ...``         manage MCP servers
``hx docs [section]``  print the shipped manual
``hx changelog [ver]`` print what shipped in each version
``hx upgrade``         self-update via uv

Print mode exists so the harness is scriptable and E2E-testable without driving
a terminal UI.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hx import __version__
from hx.paths import auth_file

USAGE = """\
hx - a terminal coding agent

Usage:
  hx                        Start the interactive TUI
  hx -p, --print PROMPT     Run one prompt headlessly and print the result
  hx resume [SESSION_ID]    Resume a previous session
  hx prompt                 Print the system prompt this directory would use
  hx mcp list|add|remove    Manage MCP servers
  hx auth [set|clear]       Show or change the OpenRouter API key
  hx docs [SECTION|--all]   Print the manual, or one section of it
  hx changelog [VERSION]    Print what shipped in each version
  hx upgrade                Update hx to the latest version
  hx --version              Show version
  hx --help                 Show this message

Options:
  --model MODEL             Override the model for this run
  --mode MODE               plan | default | acceptEdits | bypass
  --cwd PATH                Run against a different project directory
  --no-sandbox              Disable OS sandboxing (permission rules still apply)
  --system-prompt TEXT      Replace the system prompt (@path reads a file)
  --append-system-prompt TEXT
                            Append to the system prompt; repeatable (@path reads a file)
"""


DOC_COMMANDS = frozenset({"docs", "changelog"})
"""Commands that print a shipped document and exit, taking no run options."""


@dataclass(slots=True)
class ParsedArgs:
    command: str = "tui"
    prompt: str = ""
    session_id: str | None = None
    rest: tuple[str, ...] = ()
    overrides: dict[str, Any] | None = None
    cwd: Path | None = None


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns a process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)

    if "--version" in args or "-V" in args:
        print(f"hx {__version__}")
        return 0
    if "--help" in args or "-h" in args:
        print(USAGE)
        return 0

    return _dispatch(args)


def _dispatch(args: list[str]) -> int:
    try:
        parsed = parse_args(args)
    except UsageError as exc:
        print(f"error: {exc}\n\n{USAGE}", file=sys.stderr)
        return 2

    if parsed.command == "upgrade":
        return run_upgrade_command()
    if parsed.command == "mcp":
        return run_mcp_command(list(parsed.rest))
    if parsed.command == "auth":
        return run_auth_command(list(parsed.rest))
    if parsed.command == "prompt":
        return run_prompt_command(parsed)
    if parsed.command == "docs":
        return run_docs_command(list(parsed.rest))
    if parsed.command == "changelog":
        return run_changelog_command(list(parsed.rest))
    if parsed.command == "print":
        return run_print_command(parsed)
    return run_tui_command(parsed)


def parse_args(args: list[str]) -> ParsedArgs:
    # The documentation commands print and exit, so their arguments are taken
    # verbatim: `hx docs --all` is a request for a section of the manual, not a
    # run of HX with an unknown option.
    if args and args[0] in DOC_COMMANDS:
        return ParsedArgs(command=args[0], rest=tuple(args[1:]))

    parsed = ParsedArgs()
    overrides: dict[str, Any] = {}
    positional: list[str] = []

    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-p", "--print"}:
            parsed.command = "print"
            index += 1
            if index >= len(args):
                raise UsageError("-p requires a prompt")
            parsed.prompt = args[index]
        elif arg == "--model":
            index += 1
            if index >= len(args):
                raise UsageError("--model requires a value")
            overrides.setdefault("models", {})["model"] = args[index]
        elif arg == "--mode":
            index += 1
            if index >= len(args):
                raise UsageError("--mode requires a value")
            overrides.setdefault("permissions", {})["mode"] = args[index]
        elif arg == "--cwd":
            index += 1
            if index >= len(args):
                raise UsageError("--cwd requires a path")
            parsed.cwd = Path(args[index]).expanduser()
        elif arg == "--no-sandbox":
            overrides.setdefault("permissions", {})["sandbox"] = False
        elif arg == "--system-prompt":
            index += 1
            if index >= len(args):
                raise UsageError("--system-prompt requires a value")
            overrides.setdefault("prompt", {})["system"] = _prompt_value(args[index])
        elif arg == "--append-system-prompt":
            index += 1
            if index >= len(args):
                raise UsageError("--append-system-prompt requires a value")
            appends = overrides.setdefault("prompt", {}).setdefault("append", [])
            appends.append(_prompt_value(args[index]))
        elif arg.startswith("-"):
            raise UsageError(f"unknown option {arg}")
        else:
            positional.append(arg)
        index += 1

    if positional:
        head, *tail = positional
        if head in {"resume", "prompt", "mcp", "upgrade", "auth"}:
            parsed.command = head
            parsed.rest = tuple(tail)
            if head == "resume" and tail:
                parsed.session_id = tail[0]
        elif parsed.command != "print":
            raise UsageError(f"unknown command {head!r}")

    parsed.overrides = overrides or None
    return parsed


def _prompt_value(raw: str) -> str:
    """A prompt flag's value. ``@path`` reads the file, anything else is literal.

    A missing file is a usage error, not an empty prompt: silently running with
    the built-in prompt after the user asked for theirs is the worse failure.
    """
    if not raw.startswith("@"):
        return raw
    path = Path(raw[1:]).expanduser()
    try:
        return path.read_text()
    except OSError as exc:
        raise UsageError(f"cannot read prompt file {path}: {exc}") from exc


@dataclass(slots=True)
class Runtime:
    """Everything one HX run needs, wired together."""

    settings: Any
    session: Any
    bus: Any
    loop: Any
    models: Any
    auth: Any
    """:class:`~hx.auth.resolve.AuthResolver` - every route's credentials."""
    provider: Any
    shell: Any = None
    jobs: Any = None
    sandbox: Any = None
    skills: Any = None
    agents: Any = None
    mcp: Any = None
    tools: Any = None
    checkpoints: Any = None
    """:class:`~hx.core.checkpoints.CheckpointStore` - pre-images for ``/rewind``."""
    tracker: Any = None
    notices: list[str] = field(default_factory=list)
    """Startup messages for the user - shown once, in the transcript."""

    @property
    def sandbox_active(self) -> bool:
        return bool(self.sandbox is not None and self.sandbox.active)

    @property
    def sandbox_backend(self) -> str:
        return str(self.sandbox.backend.value) if self.sandbox is not None else "none"

    async def connect_mcp(self) -> list[Any]:
        """Connect MCP servers and register their tools.

        Done after the loop is built and never at import time: a slow or broken
        server should delay tool availability, not startup itself.
        """
        if self.mcp is None:
            return []
        statuses: list[Any] = await self.mcp.connect_all()
        await self.mcp.register_tools(self.tools)
        return statuses

    async def refresh_models_if_stale(self) -> None:
        """Re-fetch the model catalogue once a day.

        Without this the cache written on first run never updates, so /model
        would show last month's prices and context windows forever. A failure
        here is not worth interrupting the session for - the cached catalogue
        still works - but it is worth one line in the transcript: swallowed
        whole, a proxy's TLS interception or a rejected key looks exactly like
        an empty catalogue with no cause.
        """
        if not self.models.is_stale:
            return
        try:
            await self.models.refresh(self.auth)
        except Exception as exc:
            from hx.net import describe

            self.notices.append(f"Model catalogue refresh failed: {describe(exc)}")

    async def aclose(self) -> None:
        if self.mcp is not None:
            await self.mcp.close_all()
        if self.jobs is not None:
            await self.jobs.close_all()
        if self.shell is not None:
            await self.shell.close()
        if self.sandbox is not None:
            self.sandbox.cleanup()
        await self.provider.aclose()


def build_runtime(parsed: ParsedArgs, *, resume: str | None = None) -> Runtime:
    """Boot the stack: settings, provider, session, sandbox, tools, loop.

    Compaction, skills, subagents and MCP land in later milestones; the loop
    already accepts them, so nothing here changes when they arrive.
    """
    from hx.agents.definitions import discover as discover_agents
    from hx.agents.subagent import SubagentRunner
    from hx.auth.resolve import AuthResolver
    from hx.config import load_settings
    from hx.core.checkpoints import CheckpointStore
    from hx.core.compaction import Compactor
    from hx.core.context import ContextBuilder, build_project_context, load_system_prompt
    from hx.core.events import EventBus
    from hx.core.lateinject import Injection, InjectionRegistry
    from hx.core.loop import AgentLoop
    from hx.core.session import load_session, new_session
    from hx.git import GitWatcher, git_injector
    from hx.mcp.manager import MCPManager
    from hx.mcp.manager import load_configs as load_mcp_configs
    from hx.net import tls_notice
    from hx.paths import ensure_user_dirs, session_checkpoints_dir, session_outputs_dir
    from hx.permissions.engine import PermissionEngine, load_rules, migrate_legacy_rules
    from hx.permissions.sandbox import Sandbox, default_policy
    from hx.providers import registry
    from hx.providers.models import ModelRegistry
    from hx.skills.loader import build_index
    from hx.skills.loader import discover as discover_skills
    from hx.skills.runtime import ActiveSkills, SkillTool
    from hx.tools.bash import BackgroundJobs, PersistentShell
    from hx.tools.read import FileTracker
    from hx.tools.registry import build_default_registry
    from hx.tools.task import TaskTool
    from hx.tools.todo import TodoList, todo_injector

    ensure_user_dirs()
    settings = load_settings(parsed.cwd, parsed.overrides)
    bus_holder = EventBus()

    auth = AuthResolver()

    models = ModelRegistry()
    models.load_cache()
    model_info = models.get_or_default(settings.models.model)

    session = load_session(resume) if resume else new_session(settings.cwd, settings.models.model)

    # Built after the session because a subscription route keys its prompt
    # cache on the session id.
    provider = registry.build_provider(
        settings.models.model, auth, session_id=session.meta.session_id
    )

    sandbox = (
        Sandbox(default_policy(settings.cwd, settings.permissions.allow_network))
        if settings.permissions.sandbox
        else None
    )
    shell = PersistentShell(settings.cwd, shell=settings.bash.shell, sandbox=sandbox)
    jobs = BackgroundJobs(session_outputs_dir(session.meta.session_id) / "jobs")
    tracker = FileTracker()

    todos = TodoList()
    injections = InjectionRegistry()
    injections.register("todos", todo_injector(todos))
    injections.register("stale_files", _stale_files_injector(tracker, Injection))
    if settings.context.git_notices:
        # Registered even outside a repository: the watcher disables itself on
        # its first call, which costs one subprocess rather than a check here
        # that would have to run git anyway to be right.
        injections.register("git", git_injector(GitWatcher(settings.cwd), seen=tracker.was_read))

    # Rules come from the settings files only, read once here: `settings`
    # already merges those same files, and loading both would list and match
    # every rule twice.
    notices = migrate_legacy_rules(settings.cwd)
    if (tls := tls_notice()) is not None:
        notices.append(tls)
    permissions = PermissionEngine(
        mode=settings.permissions.mode,
        rules=load_rules(settings.cwd),
        cwd=settings.cwd,
    )

    context_builder = ContextBuilder(
        load_system_prompt(settings.cwd, settings.prompt),
        settings.cwd,
        keep_recent_turns=settings.context.keep_recent_turns,
    )

    checkpoints = CheckpointStore(session, session_checkpoints_dir(session.meta.session_id))
    tools = build_default_registry(shell, jobs, tracker, todos, bus_holder, auth, checkpoints)

    skills = {skill.name: skill for skill in discover_skills(settings.cwd)}
    if skills:
        tools.register(SkillTool(skills, ActiveSkills()))

    agents = {agent.name: agent for agent in discover_agents(settings.cwd)}
    subagents = SubagentRunner(
        definitions=agents,
        provider=provider,
        tools=tools,
        permissions=permissions,
        bus=bus_holder,
        settings=settings,
        models=models,
        parent_session_id=session.meta.session_id,
        parent_usage=session.usage,
    )
    tools.register(TaskTool(subagents))

    loop = AgentLoop(
        provider=provider,
        session=session,
        tools=tools,
        permissions=permissions,
        context=context_builder,
        compactor=Compactor(
            provider=provider,
            model=settings.models.model,
            keep_recent_turns=settings.context.keep_recent_turns,
            context=context_builder,
        ),
        injections=injections,
        bus=bus_holder,
        settings=settings,
        model_info=model_info,
        skills_index=build_index(list(skills.values())) or None,
        project_context=build_project_context(settings.cwd),
    )

    return Runtime(
        settings=settings,
        session=session,
        bus=bus_holder,
        loop=loop,
        models=models,
        auth=auth,
        provider=provider,
        shell=shell,
        jobs=jobs,
        sandbox=sandbox,
        skills=skills,
        agents=agents,
        mcp=MCPManager(load_mcp_configs(settings.cwd)),
        tools=tools,
        checkpoints=checkpoints,
        tracker=tracker,
        notices=notices,
    )


def _stale_files_injector(tracker: Any, injection_cls: Any) -> Any:
    """Warn when a file changed on disk after HX last read it.

    This belongs in late injection, not the system prompt: the set changes
    constantly, and putting it in the prefix would invalidate the cache on
    every single turn.
    """

    def inject() -> Any:
        stale = tracker.stale_files()
        if not stale:
            return None
        listed = "\n".join(f"- {path}" for path in stale[:20])
        return injection_cls(
            source="stale_files",
            text=(
                "These files changed on disk since you last read them. "
                f"Re-read before editing:\n{listed}"
            ),
            priority=20,
        )

    return inject


def prompt_for_api_key() -> bool:
    """First-run onboarding. Returns True once a key has been saved.

    The key is read without echo and written straight to ``~/.hx/auth.json``
    with mode 0600; it is never printed back or logged.
    """
    from hx.providers.openrouter import save_api_key

    if not sys.stdin.isatty():
        return False

    print("HX needs an OpenRouter API key.")
    print("Create one at https://openrouter.ai/keys, then paste it below.")
    try:
        key = getpass.getpass("OpenRouter API key (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if not key:
        return False

    save_api_key(key)
    print(f"Saved to {auth_file()} (mode 0600).")
    return True


def prompt_for_tavily_key() -> bool:
    """Save a Tavily key for WebSearch/WebFetch. Returns True once stored.

    Separate from :func:`prompt_for_api_key` because it is not onboarding: HX
    runs fine without it, and the two tools are simply absent until it exists.
    """
    from hx.auth.store import TAVILY, ApiKeyCredential, AuthStore

    if not sys.stdin.isatty():
        return False

    print("A Tavily API key turns on the WebSearch and WebFetch tools.")
    print("Create one at https://app.tavily.com - the free tier is 1000 searches a month.")
    try:
        key = getpass.getpass("Tavily API key (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if not key:
        return False

    AuthStore().save(TAVILY, ApiKeyCredential(key=key))
    print(f"Saved to {auth_file()} (mode 0600). WebSearch is on from the next `hx`.")
    return True


def _print_tavily_status(resolver: Any) -> None:
    """Web search sits below the model routes: it pays for searches, not turns."""
    from hx.auth.store import TAVILY, mask

    source = resolver.source(TAVILY)
    if source is None:
        print(f"{TAVILY:<14} not configured      WebSearch and WebFetch are off")
        return
    key = mask(resolver.resolve_static(TAVILY).token)
    print(f"{TAVILY:<14} {'key ' + key:<20} {source}")


def run_tui_command(parsed: ParsedArgs) -> int:
    """Boot the full stack - settings, provider, tools, MCP, skills - and run the TUI."""
    from hx.auth.resolve import MissingCredential
    from hx.tui.app import run_tui

    try:
        runtime = build_runtime(parsed, resume=_resume_target(parsed))
    except MissingCredential as exc:
        # Onboard for the route the configured model needs, not always for
        # OpenRouter: a user set to a Codex model wants a sign-in, not a key.
        if run_login(exc.provider_id) != 0:
            return _report(exc)
        try:
            runtime = build_runtime(parsed, resume=_resume_target(parsed))
        except Exception as retry_exc:
            return _report(retry_exc)
    except Exception as exc:
        return _report(exc)

    async def main_async() -> None:
        try:
            await runtime.connect_mcp()
            await runtime.refresh_models_if_stale()
            await run_tui(
                runtime.loop,
                runtime.bus,
                runtime.settings,
                runtime.models,
                runtime.auth,
                sandbox_active=runtime.sandbox_active,
                sandbox_backend=runtime.sandbox_backend,
                skills=runtime.skills,
                agents=runtime.agents,
                mcp=runtime.mcp,
                notices=runtime.notices,
                checkpoints=runtime.checkpoints,
                tracker=runtime.tracker,
            )
            # The TUI is down but the provider is not, which is the one moment
            # the whole session exists and nothing is competing for the screen.
            await _rename_closed_session(runtime.loop)
        finally:
            runtime.bus.close()
            await runtime.provider.aclose()

    asyncio.run(main_async())
    return 0


RENAME_TIMEOUT_SECONDS = 10.0
"""A session name is not worth making the user wait for. Exit wins the tie."""


async def _rename_closed_session(loop: Any) -> None:
    """Re-name the session now that it is over, if the work moved on.

    The first name is written after one exchange and never revisited, so
    ``/resume`` ends up listing opening questions. Bounded and swallowed: this
    runs while the user is waiting for their shell prompt back.
    """
    try:
        await asyncio.wait_for(loop.retitle_session(), timeout=RENAME_TIMEOUT_SECONDS)
    except Exception:
        # Including the timeout. A rename that cannot happen is not an error the
        # user needs at the moment they are leaving; the old name still stands.
        return


def run_print_command(parsed: ParsedArgs) -> int:
    """Headless single-prompt run. Streams assistant text to stdout and tool
    activity to stderr, so stdout stays pipeable."""
    from hx.core import events as ev

    try:
        runtime = build_runtime(parsed, resume=_resume_target(parsed))
    except Exception as exc:
        return _report(exc)

    async def main_async() -> int:
        async def render() -> None:
            async for event in runtime.bus.subscribe():
                match event:
                    case ev.TextDelta():
                        sys.stdout.write(event.text)
                        sys.stdout.flush()
                    case ev.ToolCallStarted():
                        print(f"[tool] {event.name} {event.input}", file=sys.stderr)
                    case ev.PermissionRequested():
                        print(f"[permission] {event.description}", file=sys.stderr)
                    case ev.ToolCallFinished():
                        marker = "error" if event.is_error else "ok"
                        reason = f" {event.detail}" if event.detail else ""
                        print(
                            f"[tool] {marker} ({event.duration_ms:.0f}ms){reason}",
                            file=sys.stderr,
                        )
                    case ev.ErrorRaised():
                        print(f"[error] {event.message}", file=sys.stderr)

        renderer = asyncio.create_task(render())
        await asyncio.sleep(0)
        for notice in runtime.notices:
            print(f"[hx] {notice}", file=sys.stderr)
        for status in await runtime.connect_mcp():
            if not status.connected:
                print(f"[mcp] {status.name} unavailable: {status.error}", file=sys.stderr)
        try:
            result = await runtime.loop.run(parsed.prompt)
            await _rename_closed_session(runtime.loop)
        finally:
            await asyncio.sleep(0.05)
            runtime.bus.close()
            await renderer
            await runtime.aclose()

        print()
        usage = runtime.session.usage
        print(
            f"[usage] in={usage.total_input} out={usage.total_output} "
            f"cache_read={usage.total_cache_read} cache_write={usage.total_cache_write} "
            f"cost=${usage.total_cost_usd:.4f}",
            file=sys.stderr,
        )
        return 1 if result.error else 0

    return asyncio.run(main_async())


def run_prompt_command(parsed: ParsedArgs) -> int:
    """``hx prompt`` - print the system prompt this directory resolves to.

    The prompt goes to stdout so it can be piped or diffed; where it came from
    goes to stderr so it never contaminates that output.
    """
    from hx.config import load_settings
    from hx.core.context import resolve_system_prompt

    try:
        settings = load_settings(parsed.cwd, parsed.overrides)
        resolved = resolve_system_prompt(settings.cwd, settings.prompt)
    except Exception as exc:
        return _report(exc)

    print(resolved.text)
    print(f"[source] {resolved.source}", file=sys.stderr)
    for append in resolved.appends:
        print(f"[append] {append}", file=sys.stderr)
    return 0


def run_docs_command(args: list[str]) -> int:
    """``hx docs [SECTION|--all]`` - the manual that shipped with this version.

    Bare ``hx docs`` lists the sections rather than printing the whole manual:
    it is thousands of tokens, and the caller is usually a session answering
    one question about HX itself.
    """
    from hx.docs import DocsUnavailable, doc_text, manual

    try:
        if args and args[0] in {"--all", "-a"}:
            print(doc_text("README.md").strip())
        else:
            print(manual(" ".join(args) if args else None))
    except DocsUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def run_changelog_command(args: list[str]) -> int:
    """``hx changelog [VERSION]`` - what shipped in each version.

    ``VERSION`` accepts ``0.1.5``, ``v0.1.5``, ``unreleased`` or ``latest``.
    """
    from hx.docs import DocsUnavailable, changelog

    try:
        print(changelog(args[0] if args else None))
    except DocsUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _resume_target(parsed: ParsedArgs) -> str | None:
    """Resolve ``hx resume [id]`` to a session id, newest-first when omitted."""
    if parsed.command != "resume":
        return None
    if parsed.session_id:
        return parsed.session_id

    from hx.config import load_settings
    from hx.core.session import latest_session

    settings = load_settings(parsed.cwd, parsed.overrides)
    meta = latest_session(settings.cwd)
    if meta is None:
        raise UsageError("no previous session in this directory")
    return meta.session_id


MCP_USAGE = """\
hx mcp list                                  Show configured servers
hx mcp add NAME COMMAND [ARGS...]            Add a stdio server
hx mcp add NAME --url URL                    Add an HTTP server
hx mcp remove NAME                           Remove a server

Add --user to write to ~/.hx/mcp.json instead of the project's .hx/mcp.json.
"""


def run_mcp_command(args: list[str]) -> int:
    from hx.mcp.manager import (
        MCPServerConfig,
        load_configs,
        remove_config,
        save_config,
    )

    user_level = "--user" in args
    args = [arg for arg in args if arg != "--user"]
    cwd = Path.cwd()

    if not args or args[0] == "list":
        configs = load_configs(cwd)
        if not configs:
            print("No MCP servers configured.\n")
            print(MCP_USAGE)
            return 0
        statuses = asyncio.run(_probe_servers(configs))
        for status in statuses:
            mark = "ok" if status.connected else "unavailable"
            detail = f" - {status.error}" if status.error else f" ({status.tool_count} tools)"
            print(f"{status.name:<20} {mark}{detail}")
        return 0

    if args[0] == "add":
        if len(args) < 3:
            print(MCP_USAGE, file=sys.stderr)
            return 2
        name = args[1]
        if args[2] == "--url":
            config = MCPServerConfig(name=name, transport="http", url=args[3])
        else:
            config = MCPServerConfig(
                name=name, transport="stdio", command=args[2], args=tuple(args[3:])
            )
        print(f"Added {name} to {save_config(config, cwd, user_level)}")
        return 0

    if args[0] == "remove":
        if len(args) < 2:
            print(MCP_USAGE, file=sys.stderr)
            return 2
        if remove_config(args[1], cwd, user_level):
            print(f"Removed {args[1]}.")
            return 0
        print(f"No server named {args[1]}.", file=sys.stderr)
        return 1

    print(MCP_USAGE, file=sys.stderr)
    return 2


async def _probe_servers(configs: list[Any]) -> list[Any]:
    from hx.mcp.manager import MCPManager

    manager = MCPManager(configs)
    try:
        return await manager.connect_all()
    finally:
        await manager.close_all()


AUTH_USAGE = """\
hx auth                     Show which routes have a credential
hx auth set [provider]      Paste an API key (hidden) and save it
hx auth clear [provider]    Remove a saved API key
hx auth login [provider]    Sign in - openrouter, openai-codex
hx auth logout <provider>   Forget a stored credential

Providers:
  openrouter      API key. The environment (HX_OPENROUTER_API_KEY, then
                  OPENROUTER_API_KEY) takes precedence over the saved file.
  openai-codex    ChatGPT Plus/Pro subscription, signed in over OAuth.
  tavily          API key for WebSearch and WebFetch. Not a model route:
                  it is spent per search, not per token.
"""


class ConsoleLogin:
    """Drives an OAuth flow from a plain terminal.

    The paste prompt runs on a thread so the loopback callback can win the race
    on a machine that does have a browser.
    """

    def __init__(self) -> None:
        self._url: str | None = None

    def show_url(self, url: str, instructions: str) -> None:
        self._url = url
        print(f"\n{instructions}\n\n  {url}\n")

    def show_device_code(self, user_code: str, verification_uri: str) -> None:
        print(f"\nOpen {verification_uri} and enter this code:\n\n  {user_code}\n")

    def progress(self, message: str) -> None:
        print(message)

    async def prompt_paste(self, message: str) -> str:
        if not sys.stdin.isatty():
            # Nothing to read from; let the callback server decide the outcome.
            await asyncio.Event().wait()
        return await asyncio.to_thread(input, f"{message} ")


def run_login(provider_id: str) -> int:
    """Sign in to one provider and store the credential."""
    from hx.auth.oauth import codex as codex_oauth
    from hx.auth.oauth.callback import CallbackError
    from hx.auth.store import OPENROUTER, AuthStore
    from hx.providers import registry

    if provider_id == OPENROUTER:
        return 0 if prompt_for_api_key() else 1

    try:
        spec = registry.get(provider_id)
    except registry.UnknownProvider:
        known = ", ".join(s.id for s in registry.SPECS)
        print(f"Unknown provider {provider_id!r}. Known: {known}", file=sys.stderr)
        return 2

    if provider_id != codex_oauth.PROVIDER_ID:
        print(f"{spec.label} has no interactive login.", file=sys.stderr)
        return 2

    interaction = ConsoleLogin()
    use_device = not sys.stdin.isatty() or os.environ.get("HX_LOGIN_DEVICE_CODE") == "1"
    flow = codex_oauth.login_device_code if use_device else codex_oauth.login_browser

    try:
        credential = asyncio.run(flow(interaction))
    except (codex_oauth.OAuthError, CallbackError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 1

    AuthStore().save(provider_id, credential)
    print(f"Signed in to {spec.label}. Saved to {auth_file()} (mode 0600).")
    print("Select a model with: hx --model openai-codex/gpt-5.3-codex")
    return 0


def run_auth_command(args: list[str]) -> int:
    """Show or change stored credentials.

    The TUI has /login and /configure; this is the same thing for a headless
    machine, where there is no interface to prompt from mid-session.
    """
    from hx.auth.resolve import AuthResolver
    from hx.auth.store import OPENROUTER, TAVILY, AuthStore
    from hx.providers import registry

    action = args[0] if args else "status"
    resolver = AuthResolver()

    if action == "status":
        signed_in = False
        for spec in registry.SPECS:
            source = resolver.source(spec.id)
            if source is None:
                print(f"{spec.id:<14} not signed in")
                continue
            signed_in = True
            print(f"{spec.id:<14} {_credential_label(resolver, spec):<20} {source}")
            if source.startswith("environment"):
                print(f"{'':<14} the environment overrides anything saved in {auth_file()}.")
        _print_tavily_status(resolver)
        if not signed_in:
            print(
                "\nRun `hx auth set` to save an OpenRouter key, or `hx auth login` "
                "to sign in to a subscription."
            )
            return 1
        return 0

    if action == "set":
        target = args[1] if len(args) > 1 else OPENROUTER
        if target not in {OPENROUTER, TAVILY}:
            print(
                f"{target} does not take a pasted key. Try `hx auth login {target}`.",
                file=sys.stderr,
            )
            return 2
        saved = prompt_for_tavily_key() if target == TAVILY else prompt_for_api_key()
        if not saved:
            print("No key entered.", file=sys.stderr)
            return 1
        return 0

    if action == "login":
        return run_login(args[1] if len(args) > 1 else _choose_provider())

    if action == "logout":
        if len(args) < 2:
            print(AUTH_USAGE, file=sys.stderr)
            return 2
        if AuthStore().delete(args[1]):
            print(f"Removed the {args[1]} credential from {auth_file()}.")
            return 0
        print(f"No stored credential for {args[1]}.")
        return 0

    if action == "clear":
        target = args[1] if len(args) > 1 else OPENROUTER
        if AuthStore().delete(target):
            print(f"Removed the saved {target} key from {auth_file()}.")
        else:
            print(f"No saved {target} key to remove.")
        return 0

    print(AUTH_USAGE, file=sys.stderr)
    return 2


def _credential_label(resolver: Any, spec: Any) -> str:
    """A one-word description of the credential, never the secret itself."""
    from hx.auth.resolve import ExpiredCredential, MissingCredential
    from hx.auth.store import mask

    if spec.is_subscription:
        return "signed in"
    try:
        return f"key {mask(resolver.resolve_static(spec.id).token)}"
    except (MissingCredential, ExpiredCredential):
        return "unusable"


def _choose_provider() -> str:
    """Ask which provider to sign in to. Defaults to OpenRouter when piped."""
    from hx.auth.store import OPENROUTER
    from hx.providers import registry

    if not sys.stdin.isatty():
        return OPENROUTER

    print("Sign in to:")
    for index, spec in enumerate(registry.SPECS, start=1):
        print(f"  {index}  {spec.label}")
    try:
        choice = input("Choice [1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print()
        return OPENROUTER
    try:
        return registry.SPECS[int(choice) - 1].id
    except (ValueError, IndexError):
        return OPENROUTER


def run_upgrade_command() -> int:
    """Wraps ``uv tool upgrade hx``."""
    try:
        completed = subprocess.run(["uv", "tool", "upgrade", "hx-cli"], check=False)
    except FileNotFoundError:
        print("uv is not installed. See https://docs.astral.sh/uv/", file=sys.stderr)
        return 1
    return completed.returncode


def _report(exc: Exception) -> int:
    from hx.auth.resolve import ExpiredCredential, MissingCredential

    if isinstance(exc, MissingCredential | ExpiredCredential | UsageError):
        print(f"error: {exc}", file=sys.stderr)
        return 2
    raise exc


class UsageError(Exception):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
