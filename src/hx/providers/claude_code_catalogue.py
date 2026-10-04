"""Which Claude models a Claude Code login can run.

Claude Code knows: the ``initialize`` handshake answers with the models the
signed-in account may pick, each with the reasoning efforts it accepts - the
same list its own ``/model`` shows. Asking costs a process start and no tokens.

Ids are the resolved model names (``claude-code/claude-opus-4-8``) rather than
Claude Code's aliases, so a session records which model actually answered -
with ``[1m]`` kept where Claude Code runs the model at its long context.
When the CLI cannot be asked, :data:`~hx.providers.models.CLAUDE_CODE_MODELS`
stands in with the aliases, which always resolve to something.

The list is the installed CLI's, so an old Claude Code lists old models: the
account may call Opus 5.5 while a CLI from before it offers Opus 4.8. Nothing
in the handshake says so, so :func:`update_advice` compares ``claude
--version`` with the release npm publishes and says what to run.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from hx import net
from hx.auth import claude_code as cc_auth
from hx.providers.base import ProviderError
from hx.providers.claude_code_cli import CliProcess

if TYPE_CHECKING:
    from hx.auth.resolve import ResolvedAuth
    from hx.providers.models import ModelInfo

NAMESPACE = f"{cc_auth.PROVIDER_ID}/"

CONTEXT = 200_000
LONG_CONTEXT = 1_000_000
"""For a model Claude Code lists with a ``[1m]`` suffix: its long-context tier."""
MAX_OUTPUT_TOKENS = 64_000

MEASURED: dict[str, tuple[int, int]] = {
    "claude-opus-5-5": (LONG_CONTEXT, 128_000),
    "claude-sonnet-5-5": (LONG_CONTEXT, 128_000),
    "claude-sonnet-5": (LONG_CONTEXT, 128_000),
    "claude-opus-4-7": (LONG_CONTEXT, MAX_OUTPUT_TOKENS),
    "claude-sonnet-4-6": (CONTEXT, 32_000),
    "claude-haiku-4-5": (CONTEXT, 32_000),
}
"""``(context window, max output)`` Claude Code serves a model asked for by its bare
name, where every plan it was measured on agrees.

Where a model starts, not what it is held to: the handshake lists no sizes, and the
first turn's report replaces these (:meth:`~hx.providers.models.ModelRegistry.adopt_limits`).
A window guessed too large is the costly mistake - a conversation past the real one
fails on its first turn, where one guessed too small only compacts early - so a model
served 1M on one plan and 200k on another is left out and starts at :data:`CONTEXT`:
Opus 5 and Opus 4.8 today.

Measured 2026-10-04 with Claude Code 2.1.285 on Pro without usage credits, against
pi-claude-bridge's runs on Pro with them and on Max (its ``diag/CONTEXT-SIZE.md``).
"""

EFFORT_ORDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

DATED = re.compile(r"-\d{8}$")
"""A snapshot date, which HX ids leave off: ``claude-haiku-4-5-20251001``."""

RELEASES_URL = "https://registry.npmjs.org/-/package/@anthropic-ai/claude-code/dist-tags"
"""Claude Code's release channels: ``{"stable": "2.1.285", "latest": ...}``.

The npm registry's, which every install method follows - the native installer
and both Homebrew casks publish the same versions."""
RELEASES_ENV = "HX_CLAUDE_CODE_RELEASES_URL"

RELEASE_CHANNEL = "stable"
"""What the installed CLI is compared with. Stable trails latest, so a CLI
behind it is behind on every channel, and one following latest - ahead of
stable - is never told to update to something older."""

RELEASES_TIMEOUT = 5.0
VERSION_TIMEOUT = 10.0

_VERSION = re.compile(r"\s*(\d+(?:\.\d+)+)")
_CASK = re.compile(r"/Caskroom/([^/]+)/")


async def fetch_models(auth: ResolvedAuth | None = None) -> list[ModelInfo]:
    """The models this Claude Code login offers.

    Raises:
        ProviderError: when the CLI is missing or does not answer.
    """
    return parse_models(await fetch_raw())


async def fetch_raw() -> list[dict[str, Any]]:
    from hx.providers.claude_code import CHILD_ENV

    cli = cc_auth.find_cli()
    if cli is None:
        raise ProviderError(cc_auth.missing_message())
    process = await CliProcess.spawn(
        [
            cli,
            "-p",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--verbose",
            "--tools",
            "",
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-session-persistence",
        ],
        # Any directory will do: nothing is run, only the handshake.
        cwd=str(Path.home()),
        env=cc_auth.child_env(CHILD_ENV),
        server=None,
    )
    try:
        response = await process.initialize()
    finally:
        await process.close()
    models = response.get("models")
    if not isinstance(models, list):
        raise ProviderError("Claude Code did not list any models. Update it with `claude update`.")
    return [entry for entry in models if isinstance(entry, dict)]


def parse_models(entries: list[dict[str, Any]]) -> list[ModelInfo]:
    """Catalogue entries, one per model, aliases folded into the model they name."""
    seen: dict[str, ModelInfo] = {}
    for entry in entries:
        info = parse_entry(entry)
        if info is not None and info.id not in seen:
            seen[info.id] = info
    return list(seen.values())


def parse_entry(entry: dict[str, Any]) -> ModelInfo | None:
    from hx.providers.models import CacheMode, ModelInfo, ModelPricing

    value = str(entry.get("value") or "")
    if not value or value == "default":
        # "Default" is an alias of a model listed under its own name too.
        return None
    resolved = DATED.sub("", str(entry.get("resolvedModel") or value))
    # Claude Code marks a model it runs at its long context in ``value`` -
    # ``claude-fable-5-1[1m]`` resolving to ``claude-fable-5-1`` - and asked for
    # without the mark, the model runs at its standard context instead.
    long_context = "[1m]" in resolved or value.endswith("[1m]")
    if long_context and "[1m]" not in resolved:
        resolved += "[1m]"
    window, output = MEASURED.get(resolved.removesuffix("[1m]"), (CONTEXT, MAX_OUTPUT_TOKENS))
    levels = tuple(
        level for level in EFFORT_ORDER if level in (entry.get("supportedEffortLevels") or ())
    )
    return ModelInfo(
        id=f"{NAMESPACE}{resolved}",
        name=_name(entry, resolved, long_context),
        context_window=LONG_CONTEXT if long_context else window,
        max_output_tokens=output,
        # The subscription pays; nothing is charged per token.
        pricing=ModelPricing(),
        cache_mode=CacheMode.IMPLICIT,
        supports_tools=True,
        supports_reasoning=bool(levels or entry.get("supportsAdaptiveThinking")),
        supports_images=True,
        provider_id=cc_auth.PROVIDER_ID,
        is_subscription=True,
        reasoning_levels=levels,
        # Left to Claude Code, which picks per model and per plan.
        default_reasoning_level=None,
    )


def _name(entry: dict[str, Any], resolved: str, long_context: bool) -> str:
    """``Sonnet 5``, from ``"Sonnet 5 · Efficient for routine tasks"``.

    A note the account should know before picking - "Requires usage credits" -
    is kept: on a plan where a model costs extra, the picker is where to say so.
    """
    description = str(entry.get("description") or "")
    parts = [part.strip() for part in description.split("·") if part.strip()]
    name = parts[0] if parts else str(entry.get("displayName") or resolved)
    if long_context and "1M" not in name:
        name += " (1M context)"
    notes = [part for part in parts[1:] if "credit" in part.lower() or "usage" in part.lower()]
    if notes:
        name += f" - {notes[0][0].lower()}{notes[0][1:]}"
    return name


async def installed_version() -> str | None:
    """``2.1.285``, from ``claude --version``; ``None`` when it cannot be told."""
    cli = cc_auth.find_cli()
    if cli is None:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            cli,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=cc_auth.child_env(),
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(process.communicate(), VERSION_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return None
    return _version(out.decode(errors="replace"))


async def released_version() -> str | None:
    """The current release on :data:`RELEASE_CHANNEL`.

    ``None`` when the registry cannot be asked: the answer only ever adds a
    hint, and a network that blocks npm is no reason to show an error.
    """
    url = os.environ.get(RELEASES_ENV, "").strip() or RELEASES_URL
    try:
        async with net.async_client(timeout=RELEASES_TIMEOUT) as client:
            response = await client.get(url)
            response.raise_for_status()
            channels = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    tag = channels.get(RELEASE_CHANNEL) if isinstance(channels, dict) else None
    return _version(tag) if isinstance(tag, str) else None


async def update_advice(installed: str | None) -> str | None:
    """What to run for newer models, when ``installed`` is behind the release."""
    if installed is None:
        return None
    released = await released_version()
    if released is None or _key(installed) >= _key(released):
        return None
    return (
        f"Claude Code {installed} is behind the current release, {released}, so newer "
        f"Claude models are missing. Update it: {update_command(cc_auth.find_cli())}"
    )


def update_command(cli: str | None) -> str:
    """How this install updates.

    ``claude update`` for the native installer and npm. A Homebrew cask is
    upgraded by Homebrew, under the cask's own name - ``claude-code`` follows
    stable, ``claude-code@latest`` latest - which the link on PATH resolves to.
    """
    cask = _CASK.search(os.path.realpath(cli)) if cli else None
    return f"brew upgrade --cask {cask.group(1)}" if cask else "claude update"


def _version(text: str) -> str | None:
    match = _VERSION.match(text)
    return match.group(1) if match else None


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))
