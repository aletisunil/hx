"""Which models a ChatGPT subscription can actually reach.

The Codex backend does publish a catalogue - ``/codex/models`` - and it answers
per account, so it is the only thing that knows what a given login may call.
Entitlement is not derivable from the plan name: a ``plus`` account has been
observed serving ``gpt-5.6-sol`` and refusing ``gpt-5.3-codex`` in the same
second, with the refusal phrased as though the model id were wrong.

The catalogue is asked for at login and on the usual refresh; when it cannot be
reached, :data:`~hx.providers.models.CODEX_MODELS` stands in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hx import __version__
from hx.auth.oauth.codex import ORIGINATOR
from hx.net import async_client
from hx.providers.base import ProviderError

if TYPE_CHECKING:
    from hx.auth.resolve import ResolvedAuth
    from hx.providers.models import ModelInfo

CATALOGUE_URL = "https://chatgpt.com/backend-api/codex/models"

CLIENT_VERSION = "0.160.0"
"""The Codex client version HX presents itself as, *not* HX's own version.

The backend filters the catalogue by each model's ``minimal_client_version``,
so the number decides what comes back: asking as ``0.1.8`` returns an empty
list, and asking as ``0.124.0`` hides every model shipped since. Raise it when
HX has been checked against a newer Codex client.
"""

LISTED = "list"
"""``visibility`` of a model meant for a person to pick.

The catalogue also carries internal entries - a review model, a capacity
fallback - that are reachable but are not offers.
"""

MAX_OUTPUT_TOKENS = 128_000
"""Not in the catalogue, and the same for every Codex model today."""

FALLBACK_CONTEXT = 272_000
"""For an entry whose ``context_window`` is missing or unparseable."""

EFFORT_ORDER: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
"""Reasoning efforts weakest first, as the Codex catalogue names them.

Only the order matters: an effort a model does not advertise is answered with
the strongest one it does that is no stronger than what was asked for, so a
setting of ``max`` runs at ``xhigh`` on a model that stops there instead of
being refused.
"""

HTTP_TIMEOUT = 30.0


async def fetch_models(auth: ResolvedAuth) -> list[ModelInfo]:
    """The models this login may call, as catalogue entries HX can show.

    Args:
        auth: A resolved Codex credential; ``extra["account_id"]`` selects the
            account whose entitlements are returned.

    Raises:
        ProviderError: on any response that is not a usable catalogue. The
            caller keeps the models it already had.
    """
    return [parse_entry(entry) for entry in _listed(await fetch_raw(auth))]


async def fetch_raw(auth: ResolvedAuth) -> list[dict[str, Any]]:
    """GET the catalogue, unfiltered and unmapped."""
    account_id = auth.extra.get("account_id")
    if not account_id:
        raise ProviderError(
            "No ChatGPT account id on the stored login. Run `hx auth login openai-codex`."
        )
    headers = {
        "Authorization": f"Bearer {auth.token}",
        "chatgpt-account-id": str(account_id),
        "originator": ORIGINATOR,
        "User-Agent": f"hx/{__version__}",
    }
    async with async_client(timeout=HTTP_TIMEOUT) as client:
        response = await client.get(
            CATALOGUE_URL,
            params={"client_version": CLIENT_VERSION},
            headers=headers,
        )
        if response.status_code >= 400:
            raise ProviderError(_error_message(response.status_code, response.text))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"Codex catalogue was not JSON: {exc}") from exc

    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ProviderError("Codex catalogue carried no model list.")
    return [entry for entry in models if isinstance(entry, dict)]


def _listed(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in entries if entry.get("visibility") == LISTED and entry.get("slug")]


def parse_entry(entry: dict[str, Any]) -> ModelInfo:
    """Map one catalogue entry to a :class:`~hx.providers.models.ModelInfo`.

    ``context_window`` is preferred over ``max_context_window``: the larger
    figure is a long-context tier the default request does not buy, so gauging
    against it would under-report how full the window is.
    """
    from hx.providers.models import CODEX_NAMESPACE, CacheMode, ModelInfo, ModelPricing

    slug = str(entry["slug"])
    try:
        context_window = int(entry.get("context_window") or 0)
    except (TypeError, ValueError):
        context_window = 0
    levels = parse_levels(entry.get("supported_reasoning_levels"))
    default_level = entry.get("default_reasoning_level")
    return ModelInfo(
        id=f"{CODEX_NAMESPACE}{slug}",
        name=str(entry.get("display_name") or slug),
        context_window=context_window or FALLBACK_CONTEXT,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        # Nothing per-token is charged; the subscription already paid.
        pricing=ModelPricing(),
        cache_mode=CacheMode.IMPLICIT,
        supports_tools=True,
        supports_reasoning=bool(entry.get("supported_reasoning_levels")),
        provider_id="openai-codex",
        is_subscription=True,
        reasoning_levels=levels,
        default_reasoning_level=str(default_level) if default_level else None,
    )


def parse_levels(raw: Any) -> tuple[str, ...]:
    """The efforts a catalogue entry advertises, in :data:`EFFORT_ORDER` order.

    The catalogue lists each level as an object with a description for a model
    picker; only the name is kept. Anything unrecognised is dropped rather than
    appended, since where it sits on the scale is exactly what is unknown.
    """
    if not isinstance(raw, list):
        return ()
    named = {
        str(item.get("effort")) for item in raw if isinstance(item, dict) and item.get("effort")
    }
    return tuple(effort for effort in EFFORT_ORDER if effort in named)


def resolve_effort(requested: str | None, info: ModelInfo) -> str | None:
    """The effort to send for ``info``, honouring what the model accepts.

    With nothing requested, the model's own default is used: the catalogue sets
    it per model - ``low`` on GPT-6 Astra, ``medium`` on GPT-5.6 Terra - and
    overriding that uniformly is a decision nobody asked for.

    A requested effort the model does not advertise is lowered to the strongest
    one it does. Refusing instead would turn one setting into a list of models
    it cannot be used with.
    """
    levels = info.reasoning_levels
    if not requested:
        # None when the catalogue named no default: the backend's own choice is
        # a better guess than one invented here.
        return info.default_reasoning_level
    if not levels or requested in levels:
        return requested
    try:
        ceiling = EFFORT_ORDER.index(requested)
    except ValueError:
        # An effort off the known scale: pass it through and let the backend
        # rule on it, rather than silently running at some other depth.
        return requested
    weaker = [level for level in levels if EFFORT_ORDER.index(level) <= ceiling]
    return weaker[-1] if weaker else levels[0]


def _error_message(status: int, body: str) -> str:
    if status in (401, 403):
        return (
            f"Codex rejected the credential while listing models (HTTP {status}). "
            "Run `hx auth login openai-codex` to sign in again."
        )
    return f"Codex model list failed ({status}): {body[:200]}"
