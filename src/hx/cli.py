"""Command-line entry point.

``hx``                 launch the TUI in the current directory
``hx -p "..."``        print mode: run one prompt headless, stream to stdout
``hx resume [id]``     resume a session
``hx mcp ...``         manage MCP servers
``hx upgrade``         self-update via uv

Print mode exists so the harness is scriptable and E2E-testable without driving
a terminal UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import subprocess
import sys
from dataclasses import dataclass
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
  hx mcp list|add|remove    Manage MCP servers
  hx auth [set|clear]       Show or change the OpenRouter API key
  hx upgrade                Update hx to the latest version
  hx --version              Show version
  hx --help                 Show this message

Options:
  --model MODEL             Override the model for this run
  --mode MODE               plan | default | acceptEdits | bypass
  --cwd PATH                Run against a different project directory
  --no-sandbox              Disable OS sandboxing (permission rules still apply)
"""


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
    if parsed.command == "print":
        return run_print_command(parsed)
    return run_tui_command(parsed)


def parse_args(args: list[str]) -> ParsedArgs:
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
        elif arg.startswith("-"):
            raise UsageError(f"unknown option {arg}")
        else:
            positional.append(arg)
        index += 1

    if positional:
        head, *tail = positional
        if head in {"resume", "mcp", "upgrade", "auth"}:
            parsed.command = head
            parsed.rest = tuple(tail)
            if head == "resume" and tail:
                parsed.session_id = tail[0]
        elif parsed.command != "print":
            raise UsageError(f"unknown command {head!r}")

    parsed.overrides = overrides or None
    return parsed


@dataclass(slots=True)
class Runtime:
    """Everything one HX run needs, wired together."""

    settings: Any
    session: Any
    bus: Any
    loop: Any
    models: Any
    api_key: str
    provider: Any
    shell: Any = None
    jobs: Any = None
    sandbox: Any = None
    skills: Any = None
    agents: Any = None
    mcp: Any = None
    tools: Any = None

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
        still works.
        """
        if not self.models.is_stale:
            return
        with contextlib.suppress(Exception):
            await self.models.refresh(self.api_key)

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
    from hx.config import load_settings
    from hx.core.compaction import Compactor
    from hx.core.context import ContextBuilder, build_project_context, load_system_prompt
    from hx.core.events import EventBus
    from hx.core.lateinject import Injection, InjectionRegistry
    from hx.core.loop import AgentLoop
    from hx.core.session import load_session, new_session
    from hx.mcp.manager import MCPManager
    from hx.mcp.manager import load_configs as load_mcp_configs
    from hx.paths import ensure_user_dirs, session_outputs_dir
    from hx.permissions.engine import PermissionEngine, load_rules
    from hx.permissions.sandbox import Sandbox, default_policy
    from hx.providers.models import ModelRegistry
    from hx.providers.openrouter import OpenRouterProvider, load_api_key
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

    api_key = load_api_key()
    provider = OpenRouterProvider(api_key)

    models = ModelRegistry()
    models.load_cache()
    model_info = models.get_or_default(settings.models.model)

    session = load_session(resume) if resume else new_session(settings.cwd, settings.models.model)

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

    permissions = PermissionEngine(
        mode=settings.permissions.mode,
        rules=[
            *load_rules(settings.cwd),
            *_rules_from_settings(settings.permissions),
        ],
        cwd=settings.cwd,
    )

    context_builder = ContextBuilder(
        load_system_prompt(settings.cwd),
        settings.cwd,
        keep_recent_turns=settings.context.keep_recent_turns,
    )

    tools = build_default_registry(shell, jobs, tracker, todos, bus_holder)

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
        api_key=api_key,
        provider=provider,
        shell=shell,
        jobs=jobs,
        sandbox=sandbox,
        skills=skills,
        agents=agents,
        mcp=MCPManager(load_mcp_configs(settings.cwd)),
        tools=tools,
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


def _rules_from_settings(permissions: Any) -> list[Any]:
    """Rules passed on the command line or via env, layered on top of the files."""
    from hx.permissions.engine import Decision, parse_rule

    rules: list[Any] = []
    for texts, decision in (
        (permissions.deny, Decision.DENY),
        (permissions.ask, Decision.ASK),
        (permissions.allow, Decision.ALLOW),
    ):
        rules.extend(parse_rule(str(text), "settings", decision) for text in texts)
    return rules


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


def run_tui_command(parsed: ParsedArgs) -> int:
    """Boot the full stack - settings, provider, tools, MCP, skills - and run the TUI."""
    from hx.providers.openrouter import MissingAPIKey
    from hx.tui.app import run_tui

    try:
        runtime = build_runtime(parsed, resume=_resume_target(parsed))
    except MissingAPIKey as exc:
        if not prompt_for_api_key():
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
                runtime.api_key,
                sandbox_active=runtime.sandbox_active,
                sandbox_backend=runtime.sandbox_backend,
                skills=runtime.skills,
                agents=runtime.agents,
                mcp=runtime.mcp,
            )
        finally:
            runtime.bus.close()
            await runtime.provider.aclose()

    asyncio.run(main_async())
    return 0


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
                        print(f"[tool] {marker} ({event.duration_ms:.0f}ms)", file=sys.stderr)
                    case ev.ErrorRaised():
                        print(f"[error] {event.message}", file=sys.stderr)

        renderer = asyncio.create_task(render())
        await asyncio.sleep(0)
        for status in await runtime.connect_mcp():
            if not status.connected:
                print(f"[mcp] {status.name} unavailable: {status.error}", file=sys.stderr)
        try:
            result = await runtime.loop.run(parsed.prompt)
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
hx auth              Show whether a key is set, and where it comes from
hx auth set          Paste a new key (hidden) and save it to ~/.hx/auth.json
hx auth clear        Remove the saved key

The environment (HX_OPENROUTER_API_KEY, then OPENROUTER_API_KEY) takes
precedence over the saved file.
"""


def run_auth_command(args: list[str]) -> int:
    """Show or change the stored OpenRouter key.

    The TUI has /configure; this is the same thing for a headless machine,
    where there is no interface to prompt from mid-session.
    """
    import json

    from hx.providers.openrouter import api_key_source, load_api_key, mask_api_key

    action = args[0] if args else "status"

    if action == "status":
        source = api_key_source()
        if source is None:
            print("No OpenRouter API key set. Run `hx auth set`.")
            return 1
        print(f"Key {mask_api_key(load_api_key())} from {source}")
        if source.startswith("environment"):
            print("Note: the environment overrides anything saved in ~/.hx/auth.json.")
        return 0

    if action == "set":
        if not prompt_for_api_key():
            print("No key entered.", file=sys.stderr)
            return 1
        return 0

    if action == "clear":
        path = auth_file()
        if not path.is_file():
            print("No saved key to remove.")
            return 0
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        data.pop("openrouter_api_key", None)
        path.write_text(json.dumps(data, indent=2))
        path.chmod(0o600)
        print(f"Removed the saved key from {path}.")
        return 0

    print(AUTH_USAGE, file=sys.stderr)
    return 2


def run_upgrade_command() -> int:
    """Wraps ``uv tool upgrade hx``."""
    try:
        completed = subprocess.run(["uv", "tool", "upgrade", "hx"], check=False)
    except FileNotFoundError:
        print("uv is not installed. See https://docs.astral.sh/uv/", file=sys.stderr)
        return 1
    return completed.returncode


def _report(exc: Exception) -> int:
    from hx.providers.openrouter import MissingAPIKey

    if isinstance(exc, MissingAPIKey | UsageError):
        print(f"error: {exc}", file=sys.stderr)
        return 2
    raise exc


class UsageError(Exception):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
