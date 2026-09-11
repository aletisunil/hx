"""Which provider serves a model, and how to build it.

Routing is a property of the model id, exactly as in the settings file:

* ``anthropic/claude-sonnet-4.5`` - no known provider namespace, so OpenRouter.
  Every id that worked before this module existed still routes there.
* ``openai-codex/gpt-5.6-terra`` - the ChatGPT subscription route.

There is deliberately no ``/route`` command and no auto-selection: with two
credentials installed, "which one paid for that turn" must be answerable by
reading the model id alone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from hx.auth.oauth import codex as codex_oauth
from hx.auth.resolve import AuthResolver, ResolvedAuth
from hx.auth.store import OPENROUTER

if TYPE_CHECKING:
    from hx.providers.base import Provider
    from hx.providers.models import ModelRegistry

    BuildProvider = Callable[
        ["ProviderSpec", AuthResolver, str | None, "ModelRegistry | None"], Provider
    ]
else:  # runtime: dataclass field annotations are evaluated lazily anyway
    BuildProvider = Callable

CODEX = codex_oauth.PROVIDER_ID


class AuthKind(StrEnum):
    API_KEY = "api_key"
    SUBSCRIPTION = "subscription"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: str
    label: str
    kind: AuthKind
    namespace: str | None
    """Model-id prefix that selects this provider. ``None`` means the default."""
    build: BuildProvider

    @property
    def is_subscription(self) -> bool:
        return self.kind is AuthKind.SUBSCRIPTION


def _build_openrouter(
    spec: ProviderSpec,
    resolver: AuthResolver,
    session_id: str | None,
    models: ModelRegistry | None,
) -> Provider:
    from hx.providers.openrouter import OpenRouterProvider

    # An OpenRouter key never expires, so it is resolved once at construction
    # and baked into the client, as it always has been.
    return OpenRouterProvider(resolver.resolve_static(spec.id).token)


def _build_codex(
    spec: ProviderSpec,
    resolver: AuthResolver,
    session_id: str | None,
    models: ModelRegistry | None,
) -> Provider:
    from hx.auth.resolve import MissingCredential, missing_message
    from hx.providers.codex import CodexProvider

    # Checked eagerly so a machine that is not signed in gets the onboarding
    # prompt at startup, not a failed turn a minute later.
    if not resolver.has_credential(spec.id):
        raise MissingCredential(spec.id, missing_message(spec.id))

    # Resolved per request: an OAuth access token expires mid-session, and the
    # resolver refreshes it under the store's lock when it does.
    async def token() -> ResolvedAuth:
        return await resolver.resolve(spec.id)

    # The catalogue knows what each model will reason at; without it the
    # provider keeps its own default rather than inventing one per model.
    return CodexProvider(
        token,
        session_id=session_id,
        effort=models.reasoning_effort if models is not None else None,
    )


SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id=OPENROUTER,
        label="OpenRouter API key",
        kind=AuthKind.API_KEY,
        namespace=None,
        build=_build_openrouter,
    ),
    ProviderSpec(
        id=CODEX,
        label="OpenAI (ChatGPT Plus/Pro)",
        kind=AuthKind.SUBSCRIPTION,
        namespace=f"{CODEX}/",
        build=_build_codex,
    ),
)

BY_ID: dict[str, ProviderSpec] = {spec.id: spec for spec in SPECS}

DEFAULT_PROVIDER = BY_ID[OPENROUTER]


def subscription_plan(provider_id: str, resolver: AuthResolver) -> str | None:
    """The subscription tier behind a stored login, or ``None``.

    Reported, never acted on. It is worth showing - "which account is this, and
    what is it paying for" is otherwise unanswerable without decoding a JWT by
    hand - but it does *not* predict whether the route's models can be called:
    the Codex backend has been observed refusing every model on a ``plus``
    account and on a ``free`` one, with the same error. Gating a model switch on
    this would lock out users the plan says nothing about.
    """
    if provider_id != CODEX:
        return None

    from hx.auth.store import OAuthCredential

    stored = resolver.store.read(provider_id)
    if not isinstance(stored, OAuthCredential):
        return None
    return codex_oauth.plan_of(stored)


class UnknownProvider(Exception):
    pass


def get(provider_id: str) -> ProviderSpec:
    try:
        return BY_ID[provider_id]
    except KeyError:
        raise UnknownProvider(provider_id) from None


def split_model_id(model_id: str) -> tuple[ProviderSpec, str]:
    """``("openai-codex/gpt-5.6-terra")`` -> (codex spec, ``"gpt-5.6-terra"``).

    An id with no known namespace belongs to OpenRouter, whose own ids are
    already ``vendor/model`` shaped.
    """
    for spec in SPECS:
        if spec.namespace and model_id.startswith(spec.namespace):
            return spec, model_id[len(spec.namespace) :]
    return DEFAULT_PROVIDER, model_id


def provider_for(model_id: str) -> ProviderSpec:
    return split_model_id(model_id)[0]


def build_provider(
    model_id: str,
    resolver: AuthResolver,
    *,
    session_id: str | None = None,
    models: ModelRegistry | None = None,
) -> Provider:
    """Construct the provider that serves ``model_id``.

    ``models`` is the catalogue, and is what lets a route answer per-model
    questions - how deeply to reason, for one. Omitting it costs those answers,
    not the provider.

    Raises :class:`~hx.auth.resolve.MissingCredential` when that route has no
    credential, which callers turn into onboarding rather than a stack trace.
    """
    spec = provider_for(model_id)
    return spec.build(spec, resolver, session_id, models)


def available(resolver: AuthResolver) -> list[ProviderSpec]:
    """Specs with a usable credential. Drives which models ``/model`` offers."""
    return [spec for spec in SPECS if resolver.has_credential(spec.id)]
