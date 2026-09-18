"""Model catalogue: context windows, pricing, and caching behaviour.

Populated from OpenRouter's ``/api/v1/models`` and, for each signed-in
subscription, from that backend's own per-account catalogue
(:mod:`hx.providers.codex_catalogue`, :mod:`hx.providers.devin_catalogue`).
Cached to ``~/.hx/models.json`` and refreshed on demand (``/models refresh``),
after a login, or when the cache is older than a day. The registry drives the
``/model`` picker, the context gauge, and cost fallback maths.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from hx.paths import models_cache_file

if TYPE_CHECKING:
    from hx.auth.resolve import AuthResolver, ResolvedAuth


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
    reasoning_levels: tuple[str, ...] = ()
    """Reasoning efforts this model accepts, weakest first.

    Empty means unknown, not none: only subscription catalogues publish this,
    and an unknown list is left to the backend to judge rather than guessed at."""
    default_reasoning_level: str | None = None
    """The effort the vendor picks when the caller does not. Per model - the
    same account's models differ - so it is carried rather than assumed."""
    effort_routes: tuple[tuple[str, str], ...] = ()
    """``(effort, backend model id)`` pairs, for a route that serves each
    reasoning depth as a separate model rather than as a request field.

    Devin lists ``claude-opus-5-high`` and ``claude-opus-5-max`` as models of
    their own; HX offers them as one model whose effort picks between them, so
    ``/effort`` means the same thing on every route. Empty when the model is
    called by its own id."""
    model_router: bool = False
    """The id names a server-side dispatcher, not a model: each turn has to be
    assigned a concrete model before it can be sent."""


CODEX_NAMESPACE = "openai-codex/"

CODEX_FALLBACK_CONTEXT = 200_000
CODEX_FALLBACK_OUTPUT = 64_000
"""Assumed for a Codex model HX was told about but does not ship. There is
nothing to ask for the real figures, and a conservative guess costs an accurate
context gauge rather than the use of the model."""

_CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max")
"""Shape of the fallback entries' reasoning levels; the real ones are fetched."""

#: Fallback list of models on a ChatGPT Plus/Pro subscription, used only when
#: the backend's own catalogue cannot be reached - see
#: :mod:`hx.providers.codex_catalogue`. Entitlement is per account, so this is a
#: guess about someone else's subscription; it is what HX saw last, not a promise.
CODEX_MODELS: tuple[ModelInfo, ...] = tuple(
    ModelInfo(
        id=f"{CODEX_NAMESPACE}{slug}",
        name=name,
        context_window=272_000,
        max_output_tokens=128_000,
        pricing=ModelPricing(),
        cache_mode=CacheMode.IMPLICIT,
        supports_tools=True,
        supports_reasoning=True,
        provider_id="openai-codex",
        is_subscription=True,
        reasoning_levels=levels,
        default_reasoning_level=default_level,
    )
    for slug, name, levels, default_level in (
        ("gpt-6-astra", "GPT-6-Astra", (*_CODEX_EFFORTS, "ultra"), "low"),
        ("gpt-5.6-sol", "GPT-5.6-Sol", (*_CODEX_EFFORTS, "ultra"), "low"),
        ("gpt-5.6-terra", "GPT-5.6-Terra", (*_CODEX_EFFORTS, "ultra"), "medium"),
        ("gpt-5.6-luna", "GPT-5.6-Luna", _CODEX_EFFORTS, "medium"),
        ("gpt-5.5", "GPT-5.5", _CODEX_EFFORTS[:-1], "medium"),
    )
)

DEVIN_NAMESPACE = "devin/"

#: Fallback list for a Devin subscription, used only when the backend's own
#: catalogue cannot be reached - see :mod:`hx.providers.devin_catalogue`. These
#: two are the ones every plan has been seen to include.
DEVIN_MODELS: tuple[ModelInfo, ...] = tuple(
    ModelInfo(
        id=f"{DEVIN_NAMESPACE}{slug}",
        name=name,
        context_window=200_000,
        max_output_tokens=128_000,
        pricing=ModelPricing(),
        cache_mode=CacheMode.IMPLICIT,
        supports_tools=True,
        supports_reasoning=True,
        provider_id="devin",
        is_subscription=True,
    )
    for slug, name in (("swe-1-6", "SWE-1.6"), ("swe-1-6-fast", "SWE-1.6 Fast"))
)


@dataclass(slots=True)
class _Entitlement:
    """What one subscription's backend said its login may call."""

    models: tuple[ModelInfo, ...] | None = None
    """``None`` until the backend has answered; the fallback list stands in."""
    account: str | None = None
    """Which login :attr:`models` describes. Entitlement is per account, so a
    list fetched for another login means nothing here."""
    error: str | None = None
    """Why the list could not be fetched, in one line. Not fatal - the rest of
    the catalogue still refreshes and the fallback still offers models - but
    surfaced, so a picker missing the model someone just paid for says why."""


@dataclass(frozen=True, slots=True)
class _Subscription:
    """How to ask one subscription route for its models."""

    provider_id: str
    label: str
    fallback: tuple[ModelInfo, ...]
    fetch: Callable[[ResolvedAuth], Awaitable[list[ModelInfo]]] = field(repr=False)
    account: Callable[[ResolvedAuth], str | None] = field(repr=False)


async def _fetch_codex(auth: ResolvedAuth) -> list[ModelInfo]:
    from hx.providers import codex_catalogue

    return await codex_catalogue.fetch_models(auth)


async def _fetch_devin(auth: ResolvedAuth) -> list[ModelInfo]:
    from hx.providers import devin_catalogue

    return await devin_catalogue.fetch_models(auth)


def _codex_account(auth: ResolvedAuth) -> str | None:
    return str(auth.extra.get("account_id") or "") or None


def _token_fingerprint(auth: ResolvedAuth) -> str | None:
    """Stands in for an account id on a route whose token carries none.

    A new sign-in yields a new token and so drops the cached list, even for the
    same account - a refetch, where the alternative is keeping someone else's.
    """
    return hashlib.sha256(auth.token.encode()).hexdigest()[:16] if auth.token else None


SUBSCRIPTIONS: tuple[_Subscription, ...] = (
    _Subscription("openai-codex", "Codex", CODEX_MODELS, _fetch_codex, _codex_account),
    _Subscription("devin", "Devin", DEVIN_MODELS, _fetch_devin, _token_fingerprint),
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
        self._extra_codex: list[ModelInfo] = []
        """Codex ids from settings. Merged alongside :data:`CODEX_MODELS`."""
        self._requested_effort: str | None = None
        """``models.reasoning_effort`` from settings. Clamped per model, since
        the strongest effort one model offers is off the scale on another."""
        self._entitled: dict[str, _Entitlement] = {
            sub.provider_id: _Entitlement() for sub in SUBSCRIPTIONS
        }
        """Per subscription route, what its backend said the login may call."""
        self._models: dict[str, ModelInfo] = {}
        self._fetched_at: float = 0.0
        self._loaded = False
        self.refresh_error: str | None = None
        """Why the last refresh failed, in one line, or ``None``.

        Kept so ``/model`` can say *why* the catalogue is empty: startup
        refreshes deliberately do not interrupt the session, and a silent
        failure there is indistinguishable from having no credential at all.
        """

    def subscription_errors(self) -> list[tuple[str, str]]:
        """``(route label, reason)`` for every subscription whose list failed."""
        return [
            (sub.label, error)
            for sub in SUBSCRIPTIONS
            if (error := self._entitled[sub.provider_id].error)
        ]

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
        """Fetch every route's live catalogue and rewrite the cache.

        A subscription failure is recorded but not raised: it costs the
        account's real model list, which the fallback stands in for, and there
        is no reason for it to also cost the OpenRouter catalogue that did
        arrive.
        """
        from hx.auth.store import OPENROUTER
        from hx.net import describe
        from hx.providers.openrouter import fetch_models

        # Asked for first: a broken OpenRouter key raises below, and it must not
        # also cost a subscription its model list.
        for sub in SUBSCRIPTIONS:
            await self._refresh_subscription(sub, resolver)

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

    async def _refresh_subscription(self, sub: _Subscription, resolver: AuthResolver) -> None:
        """Ask a subscription backend which models this login may actually call.

        The answer is per account and cannot be derived from the plan name, so
        it is asked for rather than assumed. An empty answer is treated as a
        failure to answer: it is what a client version the backend does not
        recognise returns, and emptying the picker is worse than showing the
        list HX already had.
        """
        from hx.net import describe

        entitled = self._entitled[sub.provider_id]
        if not resolver.has_credential(sub.provider_id):
            # Signed out: the previous account's entitlements are not ours.
            self._entitled[sub.provider_id] = _Entitlement()
            return
        try:
            auth = await resolver.resolve(sub.provider_id)
        except Exception as exc:
            entitled.error = describe(exc)
            return

        account = sub.account(auth)
        if account != entitled.account:
            # A different login, so what is cached describes somebody else's
            # subscription. Dropped before the fetch rather than after it: if
            # the fetch then fails, the shipped list is a better answer than
            # another account's entitlements.
            entitled.models = None
            entitled.account = None

        try:
            fetched = await sub.fetch(auth)
        except Exception as exc:
            entitled.error = describe(exc)
            return
        entitled.error = None
        if fetched:
            entitled.models = tuple(fetched)
            entitled.account = account

    def _merge_static(self) -> None:
        """Add the subscription routes, whose models are never in the OpenRouter fetch."""
        for sub in SUBSCRIPTIONS:
            fetched = self._entitled[sub.provider_id].models
            for info in fetched if fetched is not None else sub.fallback:
                self._models[info.id] = info
        for info in self._extra_codex:
            # Never over a fetched entry. Settings are read before the cache is,
            # so an id named in both arrives here as an escape hatch carrying
            # guessed figures and no reasoning levels - which laid over the real
            # catalogue entry would cost that model its context window and its
            # `/effort` levels.
            self._models.setdefault(info.id, info)

    def set_reasoning_effort(self, effort: str | None) -> None:
        """Ask for an effort on every model that offers one.

        ``None`` - the default - leaves each model at the depth its vendor
        chose for it, which is what the catalogue publishes per model.
        """
        self._requested_effort = effort or None

    @property
    def requested_effort(self) -> str | None:
        """What was asked for, before any model clamped it. ``None`` means each
        model is left at its own default."""
        return self._requested_effort

    def displayed_effort(self, model_id: str) -> str | None:
        """The effort to show for a model, or ``None`` when showing one would
        be a claim HX cannot back.

        A model whose levels are unknown - anything off the Codex catalogue -
        gets nothing rather than the raw setting: OpenRouter ignores the field
        entirely today, and a status bar reading ``high`` over a route that
        never sends it is worse than a status bar that is quiet.
        """
        if not self.get_or_default(model_id).reasoning_levels:
            return None
        return self.reasoning_effort(model_id)

    def reasoning_effort(self, model_id: str) -> str | None:
        """The effort to send for one model, or ``None`` to leave it to the
        backend. Providers call this per request: the model can change without
        the route changing, and the two models differ on what they accept."""
        from hx.providers.codex_catalogue import resolve_effort

        return resolve_effort(self._requested_effort, self.get_or_default(model_id))

    def add_codex_models(self, model_ids: Iterable[str]) -> list[str]:
        """Register Codex ids from settings, on top of whatever was fetched.

        The backend's catalogue is the real list, so this is now an escape
        hatch rather than the only way in: an id the account can call but the
        catalogue does not advertise, or a session with no network to ask.

        The figures are guesses: nothing here can ask how big a window an
        unknown model has. That costs an accurate context gauge, not the
        ability to use the model.

        Returns the ids added, ignoring any already known.
        """
        added: list[str] = []
        for raw in model_ids:
            bare = raw.strip()
            if not bare:
                continue
            model_id = bare if bare.startswith(CODEX_NAMESPACE) else f"{CODEX_NAMESPACE}{bare}"
            fetched = self._entitled["openai-codex"].models
            known = fetched if fetched is not None else CODEX_MODELS
            if model_id in {info.id for info in self._extra_codex} or any(
                info.id == model_id for info in known
            ):
                continue
            self._extra_codex.append(
                ModelInfo(
                    id=model_id,
                    name=f"{model_id.removeprefix(CODEX_NAMESPACE)} (ChatGPT subscription)",
                    context_window=CODEX_FALLBACK_CONTEXT,
                    max_output_tokens=CODEX_FALLBACK_OUTPUT,
                    pricing=ModelPricing(),
                    cache_mode=CacheMode.IMPLICIT,
                    supports_tools=True,
                    supports_reasoning=True,
                    provider_id="openai-codex",
                    is_subscription=True,
                )
            )
            added.append(model_id)
        if added and self._loaded:
            self._merge_static()
        return added

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
        for sub in SUBSCRIPTIONS:
            self._load_cached_subscription(sub, payload.get(_cache_key(sub)))
        for entry in payload.get("models", []):
            try:
                info = parse_model_entry(entry)
            except (KeyError, TypeError, ValueError):
                continue
            self._models[info.id] = info
        self._merge_static()

    def _load_cached_subscription(self, sub: _Subscription, payload: Any) -> None:
        """Restore one account's fetched list from the cache file.

        Kept out of the ``models`` list because these are cached per account
        and carry no pricing: they are what one login was entitled to, not a
        public catalogue. A malformed block is dropped whole and the fallback
        list stands in - half an entitlement list is worse than none.
        """
        if not isinstance(payload, dict):
            return
        entries = payload.get("models")
        if not isinstance(entries, list):
            return
        try:
            restored = tuple(_subscription_model(sub.provider_id, entry) for entry in entries)
        except (KeyError, TypeError, ValueError):
            return
        if restored:
            account = payload.get("account_id")
            self._entitled[sub.provider_id] = _Entitlement(
                models=restored, account=str(account) if account else None
            )

    def save_cache(self) -> None:
        path = models_cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
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
        for sub in SUBSCRIPTIONS:
            entitled = self._entitled[sub.provider_id]
            if entitled.models is not None:
                payload[_cache_key(sub)] = {
                    "account_id": entitled.account,
                    "models": [_subscription_entry(m) for m in entitled.models],
                }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(path)


def _cache_key(sub: _Subscription) -> str:
    """``codex`` predates the other routes and keeps its key, so an existing
    cache still restores after an upgrade."""
    return "codex" if sub.provider_id == "openai-codex" else sub.provider_id


def _subscription_entry(model: ModelInfo) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": model.id,
        "name": model.name,
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "supports_tools": model.supports_tools,
        "supports_reasoning": model.supports_reasoning,
        "levels": list(model.reasoning_levels),
        "default_level": model.default_reasoning_level,
    }
    if model.effort_routes:
        entry["effort_routes"] = [list(route) for route in model.effort_routes]
    if model.model_router:
        entry["model_router"] = True
    return entry


def _subscription_model(provider_id: str, entry: dict[str, Any]) -> ModelInfo:
    """Inverse of :func:`_subscription_entry`. Raises on a malformed entry."""
    routes = tuple((str(effort), str(uid)) for effort, uid in entry.get("effort_routes", ()))
    return ModelInfo(
        id=str(entry["id"]),
        name=str(entry.get("name") or entry["id"]),
        context_window=int(entry["context_window"]),
        max_output_tokens=int(entry["max_output_tokens"]),
        pricing=ModelPricing(),
        cache_mode=CacheMode.IMPLICIT,
        supports_tools=bool(entry.get("supports_tools", True)),
        supports_reasoning=bool(entry.get("supports_reasoning", True)),
        provider_id=provider_id,
        is_subscription=True,
        reasoning_levels=tuple(str(level) for level in entry.get("levels", ())),
        default_reasoning_level=entry.get("default_level") or None,
        effort_routes=routes,
        model_router=bool(entry.get("model_router", False)),
    )


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
    if vendor in ("openai-codex", "devin"):
        # Both cache server-side off a stable prefix; neither takes breakpoints.
        return CacheMode.IMPLICIT
    if vendor in ModelRegistry.EXPLICIT_CACHE_VENDORS:
        return CacheMode.EXPLICIT
    if vendor in ModelRegistry.IMPLICIT_CACHE_VENDORS:
        return CacheMode.IMPLICIT
    return CacheMode.NONE


class UnknownModel(Exception):
    pass
