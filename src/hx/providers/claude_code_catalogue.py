"""Which Claude models a Claude Code login can run.

Claude Code knows: the ``initialize`` handshake answers with the models the
signed-in account may pick, each with the reasoning efforts it accepts - the
same list its own ``/model`` shows. Asking costs a process start and no tokens.

Ids are the resolved model names (``claude-code/claude-opus-4-8``) rather than
Claude Code's aliases, so a session records which model actually answered.
When the CLI cannot be asked, :data:`~hx.providers.models.CLAUDE_CODE_MODELS`
stands in with the aliases, which always resolve to something.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

EFFORT_ORDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

_DATED = re.compile(r"-\d{8}$")


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
    resolved = _DATED.sub("", str(entry.get("resolvedModel") or value))
    long_context = "[1m]" in resolved
    levels = tuple(
        level for level in EFFORT_ORDER if level in (entry.get("supportedEffortLevels") or ())
    )
    return ModelInfo(
        id=f"{NAMESPACE}{resolved}",
        name=_name(entry, resolved, long_context),
        context_window=LONG_CONTEXT if long_context else CONTEXT,
        max_output_tokens=MAX_OUTPUT_TOKENS,
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
