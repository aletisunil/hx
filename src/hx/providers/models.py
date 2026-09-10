"""Model catalogue: context windows, pricing, and caching behaviour.

Populated from OpenRouter's ``/api/v1/models``, cached to
``~/.hx/models.json`` and refreshed on demand (``/models refresh``) or when the
cache is older than a day. The registry drives the ``/model`` picker, the
context gauge, and cost fallback maths.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from hx.paths import models_cache_file

if TYPE_CHECKING:
    from hx.auth.resolve import AuthResolver


class CacheMode(StrEnum):
    EXPLICIT = "explicit"
    """Requires ``cache_control`` breakpoints (Anthropic, Gemini)."""

    IMPLICIT = "implicit"
    """Caches automatically on a stable prefix (OpenAI, DeepSeek, Grok)."""

    NONE = "none"


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """USD per token, as OpenRouter reports it."""

    prompt: float = 0.0
    completion: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    reasoning: float = 0.0


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    name: str
    context_window: int
    max_output_tokens: int
    pricing: ModelPricing
    cache_mode: CacheMode = CacheMode.NONE
    supports_tools: bool = True
    supports_reasoning: bool = False
    provider_id: str = "openrouter"
    """Which route serves this model. Derived from the id's namespace."""
    is_subscription: bool = False
    """Billed to a subscription rather than per token, so cost display is
    meaningless and the picker says so instead of printing $0.00."""


#: Models reachable on a ChatGPT Plus/Pro subscription. Hard-coded because the
#: Codex backend has no catalogue endpoint to ask; figures track models.dev.
CODEX_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        id="openai-codex/gpt-5.3-codex",
        name="GPT-5.3 Codex (ChatGPT subscription)",
        context_window=400_000,
        max_output_tokens=128_000,
        pricing=ModelPricing(),
        cache_mode=CacheMode.IMPLICIT,
        supports_tools=True,
        supports_reasoning=True,
        provider_id="openai-codex",
        is_subscription=True,
    ),
    ModelInfo(
        id="openai-codex/gpt-5.3-codex-spark",
        name="GPT-5.3 Codex Spark (ChatGPT subscription)",
        context_window=128_000,
        max_output_tokens=32_000,
        pricing=ModelPricing(),
        cache_mode=CacheMode.IMPLICIT,
        supports_tools=True,
        supports_reasoning=True,
        provider_id="openai-codex",
        is_subscription=True,
    ),
)


class ModelRegistry:
    """Lookup and refresh for the model catalogue."""

    #: Vendor prefixes whose models need explicit ``cache_control`` breakpoints.
    EXPLICIT_CACHE_VENDORS: ClassVar[frozenset[str]] = frozenset({"anthropic", "google"})
    #: Vendors that cache automatically on a stable prefix.
    IMPLICIT_CACHE_VENDORS: ClassVar[frozenset[str]] = frozenset(
        {"openai", "deepseek", "x-ai", "mistralai", "moonshotai", "qwen"}
    )
    CACHE_TTL_SECONDS: ClassVar[float] = 86_400.0

    def __init__(self) -> None:
        self._models: dict[str, ModelInfo] = {}
        self._fetched_at: float = 0.0
        self._loaded = False
        self.refresh_error: str | None = None
        """Why the last refresh failed, in one line, or ``None``.

        Kept so ``/model`` can say *why* the catalogue is empty: startup
        refreshes deliberately do not interrupt the session, and a silent
        failure there is indistinguishable from having no credential at all.
        """

    def get(self, model_id: str) -> ModelInfo:
        """Raises :class:`UnknownModel` if absent even after a cache refresh."""
        if not self._loaded:
            self.load_cache()
        try:
            return self._models[model_id]
        except KeyError:
            raise UnknownModel(model_id) from None

    def get_or_default(self, model_id: str) -> ModelInfo:
        """Never raises.

        An unknown id yields a conservative placeholder rather than a crash: a
        model missing from a stale catalogue should still be usable, it just
        cannot show accurate cost or a real context gauge.
        """
        try:
            return self.get(model_id)
        except UnknownModel:
            from hx.providers.registry import provider_for

            spec = provider_for(model_id)
            return ModelInfo(
                id=model_id,
                name=model_id,
                context_window=128_000,
                max_output_tokens=8192,
                pricing=ModelPricing(),
                cache_mode=infer_cache_mode(model_id),
                provider_id=spec.id,
                is_subscription=spec.is_subscription,
            )

    def all(self) -> list[ModelInfo]:
        if not self._loaded:
            self.load_cache()
        return sorted(self._models.values(), key=lambda m: m.id)

    def search(self, query: str) -> list[ModelInfo]:
        """Fuzzy match for the ``/model`` picker, ranked by match quality."""
        return match_models(self.all(), query)

    @property
    def is_stale(self) -> bool:
        return (time.time() - self._fetched_at) > self.CACHE_TTL_SECONDS

    async def refresh(self, resolver: AuthResolver) -> None:
        """Fetch the live OpenRouter catalogue and rewrite the cache.

        Only OpenRouter has a catalogue to fetch; a user signed in to Codex
        alone still gets that route's models, which are static.
        """
        from hx.auth.store import OPENROUTER
        from hx.net import describe
        from hx.providers.openrouter import fetch_models

        fetched: dict[str, ModelInfo] = {}
        if resolver.has_credential(OPENROUTER):
            try:
                raw = await fetch_models(resolver.resolve_static(OPENROUTER).token)
            except Exception as exc:
                # Recorded and re-raised, and the catalogue we already had is
                # left alone: a refresh that cannot reach the network is no
                # reason to lose yesterday's models.
                self.refresh_error = describe(exc)
                raise
            for entry in raw:
                try:
                    info = parse_model_entry(entry)
                except (KeyError, TypeError, ValueError):
                    # One malformed catalogue entry must not cost us the whole list.
                    continue
                fetched[info.id] = info
        self._models = fetched
        self._merge_static()
        self._loaded = True
        self.refresh_error = None
        self._fetched_at = time.time()
        self.save_cache()

    def _merge_static(self) -> None:
        """Add the routes whose catalogues do not come off the wire."""
        for info in CODEX_MODELS:
            self._models[info.id] = info

    def load_cache(self) -> None:
        """Read the cache, and register the static routes either way.

        The static routes are merged even when there is no cache file, so a
        machine whose first fetch failed still sees the models it can reach
        without the network.
        """
        self._loaded = True
        self._models = {}
        payload: dict[str, Any] = {}
        path = models_cache_file()
        if path.exists():
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                payload = {}
        self._fetched_at = payload.get("fetched_at", 0.0)
        for entry in payload.get("models", []):
            try:
                info = parse_model_entry(entry)
            except (KeyError, TypeError, ValueError):
                continue
            self._models[info.id] = info
        self._merge_static()

    def save_cache(self) -> None:
        path = models_cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": self._fetched_at,
            "models": [
                {
                    "id": m.id,
                    "name": m.name,
                    "context_length": m.context_window,
                    "top_provider": {"max_completion_tokens": m.max_output_tokens},
                    "pricing": {
                        "prompt": str(m.pricing.prompt),
                        "completion": str(m.pricing.completion),
                        "input_cache_read": str(m.pricing.cache_read),
                        "input_cache_write": str(m.pricing.cache_write),
                        "internal_reasoning": str(m.pricing.reasoning),
                    },
                    "supported_parameters": (["tools"] if m.supports_tools else [])
                    + (["reasoning"] if m.supports_reasoning else []),
                }
                for m in self.all()
                if not m.is_subscription
            ],
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(path)


def match_models(models: list[ModelInfo], query: str) -> list[ModelInfo]:
    """Rank models against a query: id prefix first, then substring, then
    subsequence.

    Lives here rather than in the picker so that ``/model opus`` and typing
    ``opus`` into the picker's filter box narrow the same list the same way.
    """
    needle = query.strip().lower()
    if not needle:
        return list(models)
    scored: list[tuple[int, str, ModelInfo]] = []
    for model in models:
        haystack = f"{model.id} {model.name}".lower()
        if needle in haystack:
            score = 0 if model.id.lower().startswith(needle) else 1
        elif _subsequence(needle, haystack):
            score = 2
        else:
            continue
        scored.append((score, model.id, model))
    return [m for _, _, m in sorted(scored, key=lambda item: (item[0], item[1]))]


def parse_model_entry(entry: dict[str, Any]) -> ModelInfo:
    """Map one OpenRouter catalogue entry to a :class:`ModelInfo`."""
    pricing_raw = entry.get("pricing") or {}
    model_id = str(entry["id"])
    top = entry.get("top_provider") or {}
    context_window = int(entry.get("context_length") or top.get("context_length") or 0)
    params = entry.get("supported_parameters") or []
    return ModelInfo(
        id=model_id,
        name=str(entry.get("name") or model_id),
        context_window=context_window or 128_000,
        max_output_tokens=int(top.get("max_completion_tokens") or 0) or 8192,
        pricing=ModelPricing(
            prompt=_price(pricing_raw.get("prompt")),
            completion=_price(pricing_raw.get("completion")),
            cache_read=_price(pricing_raw.get("input_cache_read")),
            cache_write=_price(pricing_raw.get("input_cache_write")),
            reasoning=_price(pricing_raw.get("internal_reasoning")),
        ),
        cache_mode=infer_cache_mode(model_id),
        supports_tools="tools" in params,
        supports_reasoning="reasoning" in params,
    )


def _price(value: Any) -> float:
    """OpenRouter reports prices as strings in USD per token."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(ch in it for ch in needle)


def infer_cache_mode(model_id: str) -> CacheMode:
    """Derive caching behaviour from the model's vendor prefix.

    OpenRouter does not expose this in the catalogue, so it is keyed off the
    ``vendor/`` prefix and kept in one place rather than scattered through the
    provider.
    """
    vendor = model_id.split("/", 1)[0].lower()
    if vendor == "openai-codex":
        # Responses caches implicitly off the prefix plus prompt_cache_key.
        return CacheMode.IMPLICIT
    if vendor in ModelRegistry.EXPLICIT_CACHE_VENDORS:
        return CacheMode.EXPLICIT
    if vendor in ModelRegistry.IMPLICIT_CACHE_VENDORS:
        return CacheMode.IMPLICIT
    return CacheMode.NONE


class UnknownModel(Exception):
    pass
