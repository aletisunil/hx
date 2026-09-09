"""Which provider serves a model, and how to build it.

Routing is a property of the model id, exactly as in the settings file:

* ``anthropic/claude-sonnet-4.5`` - no known provider namespace, so OpenRouter.
  Every id that worked before this module existed still routes there.
* ``openai-codex/gpt-5.3-codex`` - the ChatGPT subscription route.

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

    BuildProvider = Callable[["ProviderSpec", AuthResolver, str | None], Provider]
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
) -> Provider:
    from hx.providers.openrouter import OpenRouterProvider

    # An OpenRouter key never expires, so it is resolved once at construction
    # and baked into the client, as it always has been.
    return OpenRouterProvider(resolver.resolve_static(spec.id).token)


def _build_codex(
    spec: ProviderSpec,
    resolver: AuthResolver,
    session_id: str | None,
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

    return CodexProvider(token, session_id=session_id)


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


class UnknownProvider(Exception):
    pass


def get(provider_id: str) -> ProviderSpec:
    try:
        return BY_ID[provider_id]
    except KeyError:
        raise UnknownProvider(provider_id) from None


def split_model_id(model_id: str) -> tuple[ProviderSpec, str]:
    """``("openai-codex/gpt-5.3-codex")`` -> (codex spec, ``"gpt-5.3-codex"``).

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
) -> Provider:
    """Construct the provider that serves ``model_id``.

    Raises :class:`~hx.auth.resolve.MissingCredential` when that route has no
    credential, which callers turn into onboarding rather than a stack trace.
    """
    spec = provider_for(model_id)
    return spec.build(spec, resolver, session_id)


def available(resolver: AuthResolver) -> list[ProviderSpec]:
    """Specs with a usable credential. Drives which models ``/model`` offers."""
    return [spec for spec in SPECS if resolver.has_credential(spec.id)]
