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
from typing import Any, ClassVar

from hx.paths import models_cache_file


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

    def get(self, model_id: str) -> ModelInfo:
        """Raises :class:`UnknownModel` if absent even after a cache refresh."""
        if not self._models:
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
            return ModelInfo(
                id=model_id,
                name=model_id,
                context_window=128_000,
                max_output_tokens=8192,
                pricing=ModelPricing(),
                cache_mode=infer_cache_mode(model_id),
            )

    def all(self) -> list[ModelInfo]:
        if not self._models:
            self.load_cache()
        return sorted(self._models.values(), key=lambda m: m.id)

    def search(self, query: str) -> list[ModelInfo]:
        """Fuzzy match for the ``/model`` picker, ranked by match quality."""
        needle = query.strip().lower()
        if not needle:
            return self.all()
        scored: list[tuple[int, str, ModelInfo]] = []
        for model in self.all():
            haystack = f"{model.id} {model.name}".lower()
            if needle in haystack:
                score = 0 if model.id.lower().startswith(needle) else 1
            elif _subsequence(needle, haystack):
                score = 2
            else:
                continue
            scored.append((score, model.id, model))
        return [m for _, _, m in sorted(scored, key=lambda item: (item[0], item[1]))]

    @property
    def is_stale(self) -> bool:
        return (time.time() - self._fetched_at) > self.CACHE_TTL_SECONDS

    async def refresh(self, api_key: str) -> None:
        """Fetch the live catalogue and rewrite the cache."""
        from hx.providers.openrouter import fetch_models

        raw = await fetch_models(api_key)
        self._models = {}
        for entry in raw:
            try:
                info = parse_model_entry(entry)
            except (KeyError, TypeError, ValueError):
                # One malformed catalogue entry must not cost us the whole list.
                continue
            self._models[info.id] = info
        self._fetched_at = time.time()
        self.save_cache()

    def load_cache(self) -> None:
        path = models_cache_file()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        self._fetched_at = payload.get("fetched_at", 0.0)
        self._models = {}
        for entry in payload.get("models", []):
            try:
                info = parse_model_entry(entry)
            except (KeyError, TypeError, ValueError):
                continue
            self._models[info.id] = info

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
            ],
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(path)


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
    if vendor in ModelRegistry.EXPLICIT_CACHE_VENDORS:
        return CacheMode.EXPLICIT
    if vendor in ModelRegistry.IMPLICIT_CACHE_VENDORS:
        return CacheMode.IMPLICIT
    return CacheMode.NONE


class UnknownModel(Exception):
    pass
