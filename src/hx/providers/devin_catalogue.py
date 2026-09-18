"""Which models a Devin subscription can reach.

The Cascade backend answers ``GetCliModelConfigs`` per account, so - as with
Codex - the live list is asked for at sign-in and on the usual refresh, and
:data:`~hx.providers.models.DEVIN_MODELS` stands in when it cannot be.

Devin publishes a reasoning depth as a model of its own: "Claude Opus 5 High"
and "Claude Opus 5 Max" are separate configs that share family metadata. Those
families are folded into one :class:`~hx.providers.models.ModelInfo` whose
``effort_routes`` say which backend id serves each depth, so ``/effort`` works
here exactly as it does on Codex.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hx.net import async_client
from hx.providers.base import ProviderError
from hx.providers.codex_catalogue import EFFORT_ORDER
from hx.providers.devin_wire import (
    DEFAULT_BASE_URL,
    DISPLAY_INTERNAL_DEFAULT,
    DISPLAY_MODEL_ROUTER,
    DISPLAY_QUICK_REVIEW,
    MODEL_CONFIGS_PATH,
    UNARY_HEADERS,
    ModelConfig,
    model_configs_request,
    parse_model_configs,
    unary_error,
)
from hx.providers.protowire import WireError

if TYPE_CHECKING:
    from hx.auth.resolve import ResolvedAuth
    from hx.providers.models import ModelInfo

NAMESPACE = "devin/"

DEFAULT_CONTEXT = 200_000
DEFAULT_OUTPUT = 64_000

HTTP_TIMEOUT = 30.0

INTERNAL_DISPLAYS = frozenset({DISPLAY_QUICK_REVIEW, DISPLAY_INTERNAL_DEFAULT})
"""Display slots the server only sends because they were asked for: a review
model and internal defaults. Reachable, but not offers."""

_EFFORT_BY_NAME = {
    "none": "none",
    "nothinking": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}
_EFFORT_KEYS = frozenset({"effort", "reasoning effort"})
_FAST_KEY = "fast mode"
_THINKING_KEY = "thinking"
_CONTEXT_1M_KEY = "1m context"
"""Family axes. Fast service and a 1M window are separate models in HX - they
change what a turn costs and how much fits - while effort is the one axis
``/effort`` selects along."""


async def fetch_models(auth: ResolvedAuth, *, base_url: str = DEFAULT_BASE_URL) -> list[ModelInfo]:
    """The models this login may call, ready for the picker.

    Raises:
        ProviderError: on any answer that is not a usable catalogue, including
            an empty one - which is what a CLI identity the backend no longer
            recognises gets back. The caller keeps what it already had.
    """
    from hx.providers.devin import request_user_jwt

    async with async_client(timeout=HTTP_TIMEOUT) as client:
        # Asked first for the API server: an enterprise tenant's catalogue lives
        # on its own host, and the shared one would list the wrong entitlements.
        session = await request_user_jwt(client, base_url.rstrip("/"), auth.token)
        api = session.base_url or base_url.rstrip("/")
        response = await client.post(
            f"{api}{MODEL_CONFIGS_PATH}",
            content=model_configs_request(auth.token),
            headers=UNARY_HEADERS,
        )
    if response.status_code >= 400:
        raise ProviderError(_error_message(response.status_code, response.content))
    try:
        configs = parse_model_configs(response.content)
    except WireError as exc:
        raise ProviderError(f"Devin model list was not readable: {exc}") from exc
    models = normalize(configs)
    if not models:
        raise ProviderError(
            "Devin listed no models for this account. The CLI identity HX presents "
            "may be out of date."
        )
    return models


def normalize(configs: list[ModelConfig]) -> list[ModelInfo]:
    """Filter, map and fold the raw configs into picker entries, sorted by id."""
    kept: list[ModelConfig] = []
    seen: set[str] = set()
    for config in configs:
        if config.disabled or config.display_option in INTERNAL_DISPLAYS:
            continue
        if not config.uid or config.uid in seen:
            continue
        seen.add(config.uid)
        kept.append(config)

    lanes: dict[str, _Lane] = {}
    for config in kept:
        if not _is_router(config):
            # A router is a dispatcher, not an effort tier, even when filed
            # under a family.
            _file_under_lane(lanes, config)

    by_uid = {config.uid: config for config in kept}
    folded: set[str] = set()
    models: dict[str, ModelInfo] = {}
    for lane in lanes.values():
        info = _family_info(lane, by_uid)
        if info is None:
            continue
        folded.update(uid for _, uid in info.effort_routes)
        models[info.id] = info

    for config in kept:
        if config.uid in folded:
            continue
        info = _standalone_info(config)
        # A family can take the id of one of its own members; the family wins.
        models.setdefault(info.id, info)
    return sorted(models.values(), key=lambda m: m.id)


def _is_router(config: ModelConfig) -> bool:
    return config.display_option == DISPLAY_MODEL_ROUTER or config.is_model_router


def _is_assign_router(config: ModelConfig) -> bool:
    """A router resolved per turn through ``AssignModel``.

    ``is_model_router`` also marks harness-backed composites that are valid chat
    ids in their own right; sending one of those to ``AssignModel`` 404s.
    """
    return _is_router(config) and not config.harness_uids


def _standalone_info(config: ModelConfig) -> ModelInfo:
    from hx.providers.models import CacheMode, ModelInfo, ModelPricing

    return ModelInfo(
        id=f"{NAMESPACE}{config.uid}",
        name=config.label or config.uid,
        context_window=config.max_tokens if config.max_tokens > 0 else DEFAULT_CONTEXT,
        max_output_tokens=(
            config.max_output_tokens if config.max_output_tokens > 0 else DEFAULT_OUTPUT
        ),
        # Credits, not dollars per token: the subscription already paid.
        pricing=ModelPricing(),
        cache_mode=CacheMode.IMPLICIT,
        # Routers ship no features; Cascade only serves tool-calling models.
        supports_tools=config.supports_tools if config.has_features else True,
        supports_reasoning=_supports_thinking(config),
        provider_id="devin",
        is_subscription=True,
        model_router=_is_assign_router(config),
    )


_REASONING_LABEL = re.compile(r"think|minimal|high|medium|low|xhigh|max|reasoning", re.I)
_NO_REASONING_LABEL = re.compile(r"\bno thinking\b", re.I)


def _supports_thinking(config: ModelConfig) -> bool:
    """Server features decide; the label is read only when there are none."""
    if config.has_features:
        return config.supports_thinking
    if _NO_REASONING_LABEL.search(config.label):
        return False
    return bool(_REASONING_LABEL.search(config.label))


@dataclass(slots=True)
class _Lane:
    id: str
    name: str
    members: list[str] = field(default_factory=list)
    default_member: str | None = None
    routes: dict[str, str] = field(default_factory=dict)
    """HX effort -> member uid. The first member to claim an effort keeps it."""


def _normalize_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _file_under_lane(lanes: dict[str, _Lane], config: ModelConfig) -> None:
    if not config.family_label:
        return
    effort: str | None = None
    thinking: bool | None = None
    fast = False
    wide = False
    for entry in config.family_entries:
        key = _normalize_key(entry.key)
        if key == _FAST_KEY:
            fast = entry.order == 1
        elif key == _THINKING_KEY:
            thinking = entry.order == 1
        elif key == _CONTEXT_1M_KEY:
            wide = entry.order == 1
        elif key in _EFFORT_KEYS:
            effort = _EFFORT_BY_NAME.get(re.sub(r"[^a-z0-9]", "", entry.name.lower()))
    # Claude pairs a thinking and a non-thinking config under the same effort
    # label; the explicit Thinking axis is what tells them apart.
    if thinking is False:
        effort = "none"

    base = re.sub(r"[^a-z0-9]+", "-", config.family_label.lower()).strip("-")
    if not base:
        return
    lane_id = f"{base}{'-1m' if wide else ''}{'-fast' if fast else ''}"
    lane = lanes.get(lane_id)
    if lane is None:
        name = f"{config.family_label}{' 1M' if wide else ''}{' Fast' if fast else ''}"
        lane = lanes[lane_id] = _Lane(id=lane_id, name=name)
    lane.members.append(config.uid)
    if lane.default_member is None and config.is_default_in_family:
        lane.default_member = config.uid
    if effort is not None:
        lane.routes.setdefault(effort, config.uid)


def _family_info(lane: _Lane, by_uid: dict[str, ModelConfig]) -> ModelInfo | None:
    """Fold a lane into one model, or ``None`` when it has no effort ladder.

    A lane whose only route is "no thinking" has nothing for ``/effort`` to
    choose between, so its members stay separate models.
    """
    from dataclasses import replace

    levels = tuple(level for level in EFFORT_ORDER if level in lane.routes)
    if not any(level != "none" for level in levels):
        return None

    routed = set(lane.routes.values())
    # The server's own default when it routes somewhere; otherwise the first
    # member it listed, which is the order the CLI's picker shows them in.
    default_uid = (
        lane.default_member
        if lane.default_member in routed
        else next(uid for uid in lane.members if uid in routed)
    )
    default_level = next(level for level in levels if lane.routes[level] == default_uid)
    template = _standalone_info(by_uid[default_uid])
    return replace(
        template,
        id=f"{NAMESPACE}{lane.id}",
        name=lane.name,
        supports_reasoning=True,
        model_router=False,
        reasoning_levels=levels,
        default_reasoning_level=default_level,
        effort_routes=tuple((level, lane.routes[level]) for level in levels),
    )


def wire_model(info: ModelInfo, effort: str | None) -> str:
    """The backend id to send for ``info`` at ``effort``.

    ``effort`` is expected to have been clamped to the model's levels already
    (:meth:`~hx.providers.models.ModelRegistry.reasoning_effort` does), so an
    effort with no route only happens for a model HX knows nothing about.
    """
    routes = dict(info.effort_routes)
    if routes:
        for candidate in (effort, info.default_reasoning_level):
            if candidate and candidate in routes:
                return routes[candidate]
        return info.effort_routes[0][1]
    return info.id.removeprefix(NAMESPACE)


def _error_message(status: int, body: bytes) -> str:
    error = unary_error(body)
    detail = error.message if error is not None else body[:200].decode(errors="replace")
    if status in (401, 403):
        return (
            f"Devin rejected the credential while listing models (HTTP {status}). "
            "Run `hx auth login devin` to sign in again."
        )
    return f"Devin model list failed ({status}): {detail}"
