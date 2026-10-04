"""The Claude Code route's credential: Claude Code's own login.

HX never holds a Claude token. The ``claude-code`` route runs the official
``claude`` CLI, which signs in, stores and refreshes its credential itself
(the macOS Keychain, or ``~/.claude/.credentials.json``), so "is this route
usable" is a question for the CLI: is it installed, and does ``claude auth
status`` say it is signed in to a Claude subscription.

Every child process HX starts here gets a scrubbed environment. A variable such
as ``ANTHROPIC_API_KEY`` or ``ANTHROPIC_BASE_URL`` silently moves Claude Code
off the subscription - onto per-token billing, or onto someone else's gateway -
and the model id ``claude-code/...`` promises the subscription paid.

The same environment carries HX's TLS policy (:func:`hx.net.node_tls_env`):
the CLI makes its own connections, so a CA bundle or ``HX_SSL_NO_VERIFY`` that
HX's clients honour has to be restated for it in Node's terms.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hx import net

if TYPE_CHECKING:
    from hx.auth.oauth.browser import LoginInteraction

PROVIDER_ID = "claude-code"

CLI_ENV = "HX_CLAUDE_CODE_PATH"
"""Points HX at a ``claude`` binary that is not the one on ``PATH``."""

INSTALL_HINT = (
    "Install it from https://claude.com/claude-code, then run `hx auth login claude-code`."
)

STATUS_TTL = 30.0
"""How long one ``claude auth status`` answer is trusted.

Asked whenever ``/model`` decides which routes to offer, so it is cached; short
enough that signing in or out in another terminal shows up without a restart."""

PREPARE_AGE = STATUS_TTL / 2
"""How old an answer :func:`status_async` replaces.

Younger than the TTL, so the synchronous reads that follow it in the same
command - ``/model`` filtering routes, then building the chosen one - still
find it fresh and never start the CLI on the event loop themselves."""

STATUS_TIMEOUT = 15.0

_KEEP = frozenset(
    {
        # A long-lived subscription token from `claude setup-token`: the way a
        # headless machine signs in, and still the subscription.
        "CLAUDE_CODE_OAUTH_TOKEN",
        # Where the user keeps their Claude Code state, credentials included.
        "CLAUDE_CONFIG_DIR",
        # How the CLI's TLS is set up on a corporate network: which CA stores
        # it trusts, and the client certificate a network that demands one
        # gets. They decide whether a connection is allowed, not where it goes
        # or who pays for it.
        "CLAUDE_CODE_CERT_STORE",
        "CLAUDE_CODE_CLIENT_CERT",
        "CLAUDE_CODE_CLIENT_KEY",
        "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    }
)

_SCRUB_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")
_SCRUB_NAMES = frozenset({"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"})


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a ``claude`` child: HX's own, minus anything that
    would bill somewhere other than the subscription, plus HX's TLS policy.

    Dropped: every ``ANTHROPIC_*`` variable (an API key, an auth token, a base
    URL, a gateway's custom headers), every ``CLAUDE_CODE_*`` one bar the
    setup token and the TLS settings (Bedrock, Vertex and Foundry switches;
    and the session markers a parent Claude Code leaves behind when HX itself
    runs inside one, which would make the child believe it is nested).
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if name in _KEEP or not (name.startswith(_SCRUB_PREFIXES) or name in _SCRUB_NAMES)
    }
    env.update(net.node_tls_env(env))
    if extra:
        env.update(extra)
    return env


def find_cli() -> str | None:
    """The ``claude`` executable to run, or ``None`` when there is none."""
    override = _override()
    if override:
        return override if os.path.isfile(override) and os.access(override, os.X_OK) else None
    return shutil.which("claude")


def _override() -> str | None:
    value = os.environ.get(CLI_ENV, "").strip()
    return os.path.expanduser(value) if value else None


@dataclass(frozen=True, slots=True)
class LoginStatus:
    """What ``claude auth status`` reports, minus anything secret."""

    logged_in: bool
    auth_method: str = ""
    email: str | None = None
    organization: str | None = None
    org_id: str | None = None
    plan: str | None = None
    """``pro``, ``max``, ``team``... as Claude Code names it."""

    @property
    def is_subscription(self) -> bool:
        """Signed in to claude.ai, rather than to the Console (per-token billing).

        A setup token reports itself as an OAuth token; both are the
        subscription. Only an explicit Console login is not.
        """
        return self.logged_in and self.auth_method != "console"

    @property
    def account(self) -> str | None:
        return self.email or self.organization


_cache: tuple[float, str | None, LoginStatus | None] | None = None
_query_lock = threading.Lock()
"""One ``claude auth status`` at a time: a second caller waits for the answer
the first is already getting instead of starting another CLI."""


def status(*, fresh: bool = False) -> LoginStatus | None:
    """The CLI's login state, or ``None`` when the CLI is missing or broken.

    Cached for :data:`STATUS_TTL`; ``fresh`` asks again, which a login does.
    Blocks while the CLI answers, which can take seconds: code on the event
    loop awaits :func:`status_async` first, so that this answers from memory.
    """
    global _cache
    cli = find_cli()
    if not fresh:
        cached = _cached(cli, STATUS_TTL)
        if cached is not None:
            return cached[0]
    asked = time.monotonic()
    with _query_lock:
        if _cache is not None and _cache[1] == cli and _cache[0] >= asked:
            # Another caller started asking after this one did, and has the
            # answer: nothing older than this call's question is reused.
            return _cache[2]
        started = time.monotonic()
        result = _query_status(cli) if cli else None
        # Stamped with when the question was put, not answered: a sign-in that
        # lands while the CLI is answering is newer than this answer.
        _cache = (started, cli, result)
    return result


async def status_async(*, fresh: bool = False) -> LoginStatus | None:
    """:func:`status`, asked off the event loop.

    Replaces an answer older than :data:`PREPARE_AGE`, so the synchronous
    checks that follow it - :func:`signed_in`, a route being built - read it
    from memory rather than stalling the screen on the CLI.
    """
    if not fresh:
        cached = _cached(find_cli(), PREPARE_AGE)
        if cached is not None:
            return cached[0]
    return await asyncio.to_thread(status, fresh=True)


def _cached(cli: str | None, max_age: float) -> tuple[LoginStatus | None] | None:
    """The cached answer for ``cli`` if younger than ``max_age``, boxed so that
    a cached ``None`` (no usable CLI) is told apart from no answer at all."""
    entry = _cache
    if entry is None:
        return None
    stamp, cached_cli, cached = entry
    if cached_cli != cli or time.monotonic() - stamp >= max_age:
        return None
    return (cached,)


def forget_status() -> None:
    global _cache
    _cache = None


def _query_status(cli: str) -> LoginStatus | None:
    try:
        completed = subprocess.run(
            [cli, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT,
            env=child_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_status(completed.stdout)


def parse_status(raw: str) -> LoginStatus | None:
    """Parse ``claude auth status --json``. ``None`` for anything else."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    def text(key: str) -> str | None:
        value = payload.get(key)
        return str(value) if value else None

    return LoginStatus(
        logged_in=bool(payload.get("loggedIn")),
        auth_method=str(payload.get("authMethod") or ""),
        email=text("email"),
        organization=text("orgName"),
        org_id=text("orgId"),
        plan=text("subscriptionType"),
    )


def signed_in() -> bool:
    current = status()
    return current is not None and current.is_subscription


def missing_message() -> str:
    """Why the route cannot be used right now, and what to run."""
    if find_cli() is None:
        override = _override()
        if override:
            return (
                f"{CLI_ENV} is set to {override}, which is not an executable `claude`. "
                f"Point it at Claude Code's `claude`, or unset it to use the one on PATH."
            )
        return f"Claude Code is not installed (no `claude` on PATH). {INSTALL_HINT}"
    current = status()
    if current is not None and current.logged_in and not current.is_subscription:
        return (
            "Claude Code is signed in to the Anthropic Console, which bills per token. "
            "The claude-code route runs on a Claude subscription: run "
            "`hx auth login claude-code` to sign in with your Claude account."
        )
    return (
        "Claude Code is not signed in. Run `hx auth login claude-code`, or `/login` "
        "inside HX. Requires a Claude Pro or Max subscription."
    )


def login_terminal() -> int:
    """Sign in with Claude Code's own flow, on this terminal.

    Claude Code opens the browser, waits on its loopback callback and accepts a
    pasted code over SSH; HX has nothing to add, so it hands over the terminal.
    """
    cli = find_cli()
    if cli is None:
        print(f"Claude Code is not installed. {INSTALL_HINT}")
        return 1
    try:
        completed = subprocess.run([cli, "auth", "login", "--claudeai"], env=child_env())
    except KeyboardInterrupt:
        return 1
    forget_status()
    return completed.returncode


_URL = re.compile(r"https://\S+")
_PASTE_PROMPT = re.compile(r"^.*Paste code[^>]*>\s*")
_FAILED = re.compile(r"^Login failed:\s*", re.IGNORECASE)


def _failure(output: list[str]) -> str | None:
    """Why ``claude auth login`` failed: its last line of its own, ``unsupported
    certificate purpose`` from ``Login failed: unsupported certificate purpose``.

    Not the URL it printed before - the screen already showed that, and it is
    wider than the notice - nor the paste prompt the reason is printed after.
    """
    for line in reversed(output):
        line = _FAILED.sub("", _PASTE_PROMPT.sub("", line)).strip()
        if line and not _URL.search(line):
            return line
    return None


class LoginFailed(Exception):
    pass


async def login_interactive(interaction: LoginInteraction) -> LoginStatus:
    """Sign in through Claude Code with HX drawing the prompts (the TUI).

    ``claude auth login`` on a pipe prints the sign-in URL, opens the browser
    itself, and then waits for either the browser's loopback callback or a code
    pasted on stdin - so the URL goes to ``interaction`` and its paste prompt
    races the callback, exactly as HX's own OAuth flows do.
    """
    cli = find_cli()
    if cli is None:
        raise LoginFailed(f"Claude Code is not installed. {INSTALL_HINT}")

    process = await asyncio.create_subprocess_exec(
        cli,
        "auth",
        "login",
        "--claudeai",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=child_env(),
    )
    assert process.stdout is not None and process.stdin is not None
    output: list[str] = []
    paste: asyncio.Task[None] | None = None

    async def relay_paste() -> None:
        code = await interaction.prompt_paste("Paste the code from the browser:")
        if code.strip() and process.stdin is not None and not process.stdin.is_closing():
            process.stdin.write((code.strip() + "\n").encode())
            await process.stdin.drain()

    try:
        buffer = ""
        while True:
            chunk = await process.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk.decode(errors="replace")
            *lines, buffer = buffer.split("\n")
            # The paste prompt has no newline after it; it is what says the
            # URL has been printed and the CLI is waiting.
            if "Paste code" in buffer:
                lines.append(buffer)
                buffer = ""
            for line in lines:
                output.append(line)
                match = _URL.search(line)
                if match and paste is None:
                    interaction.show_url(
                        match.group(0),
                        "Sign in to your Claude account in the browser. If it did not "
                        "open, visit this URL and paste the code it shows.",
                    )
                    paste = asyncio.ensure_future(relay_paste())
        code = await process.wait()
    finally:
        if paste is not None:
            paste.cancel()
        if process.returncode is None:
            process.kill()
            await process.wait()
    forget_status()

    if code != 0:
        reason = _failure(output) or f"exit {code}"
        raise LoginFailed(net.with_tls_hint(f"`claude auth login` failed: {reason}"))
    current = await status_async(fresh=True)
    if current is None or not current.logged_in:
        raise LoginFailed("Claude Code still reports no login.")
    return current
