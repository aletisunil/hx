"""Turning a stored credential into request auth.

The rule, per provider id:

* ``openrouter`` keeps HX's long-standing precedence - the environment wins
  over the saved file. Changing that would silently switch which key an
  existing user's session runs on.
* Everything else follows the safer order: a stored credential owns the
  provider, and the environment is consulted only when nothing is stored. In
  particular a *failed* refresh does not quietly fall back to an env var - the
  user gets told their login expired instead of a confusing bill.

OAuth tokens are refreshed inside :meth:`AuthStore.modify`, so two turns
starting at once cannot both spend the same refresh token.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from hx.auth.oauth import codex as codex_oauth
from hx.auth.store import (
    OPENROUTER,
    TAVILY,
    ApiKeyCredential,
    AuthStore,
    Credential,
    OAuthCredential,
)

#: Environment variables consulted per provider, highest priority first.
PROVIDER_ENV: dict[str, tuple[str, ...]] = {
    OPENROUTER: ("HX_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    TAVILY: ("HX_TAVILY_API_KEY", "TAVILY_API_KEY"),
}

#: Providers whose environment variable wins over the saved credential.
ENV_FIRST = frozenset({OPENROUTER})

Refresher = Callable[[OAuthCredential], Awaitable[OAuthCredential]]

#: How to renew an expiring OAuth credential, per provider.
REFRESHERS: dict[str, Refresher] = {
    codex_oauth.PROVIDER_ID: codex_oauth.refresh,
}


class MissingCredential(Exception):
    """No usable credential for a provider. Callers turn this into onboarding."""

    def __init__(self, provider_id: str, message: str) -> None:
        super().__init__(message)
        self.provider_id = provider_id


class ExpiredCredential(Exception):
    """A stored login could not be renewed. The user has to sign in again."""

    def __init__(self, provider_id: str, message: str) -> None:
        super().__init__(message)
        self.provider_id = provider_id


@dataclass(frozen=True, slots=True)
class ResolvedAuth:
    """Everything a provider needs to authenticate one request."""

    token: str
    source: str
    """Human-readable origin, for ``/login`` and status output. Never the secret."""
    extra: dict[str, Any] = field(default_factory=dict)
    """Provider-specific companions to the token, such as a ChatGPT account id."""


class AuthResolver:
    def __init__(self, store: AuthStore | None = None) -> None:
        self.store = store if store is not None else AuthStore()

    async def resolve(self, provider_id: str) -> ResolvedAuth:
        """The token to send now, refreshing it first if it is about to expire."""
        ready, stale = self._peek(provider_id)
        if ready is not None:
            return ready
        if stale is None:
            raise MissingCredential(provider_id, missing_message(provider_id))

        refreshed = await self._refresh(provider_id, stale)
        return ResolvedAuth(
            token=refreshed.access,
            source=f"{provider_id} login",
            extra=dict(refreshed.extra),
        )

    def resolve_static(self, provider_id: str) -> ResolvedAuth:
        """Synchronous resolution for credentials that cannot expire.

        Used where there is no event loop to await a refresh on - notably
        ``build_runtime``. An OAuth credential that needs renewing raises
        rather than blocking, because a provider that can expire should be
        built around the async :meth:`resolve` instead.
        """
        ready, stale = self._peek(provider_id)
        if ready is not None:
            return ready
        if stale is not None:
            raise ExpiredCredential(
                provider_id,
                f"The {provider_id} login has expired. Run `hx auth login {provider_id}`.",
            )
        raise MissingCredential(provider_id, missing_message(provider_id))

    def _peek(self, provider_id: str) -> tuple[ResolvedAuth | None, OAuthCredential | None]:
        """Resolve without any network call.

        Returns ``(usable auth, credential needing refresh)`` - exactly one of
        the two is set when a credential exists at all.
        """
        if provider_id in ENV_FIRST:
            from_env = self._from_env(provider_id)
            if from_env is not None:
                return from_env, None

        stored = self.store.read(provider_id)
        if isinstance(stored, ApiKeyCredential):
            return (
                ResolvedAuth(
                    token=stored.key,
                    source=str(self.store.path),
                    extra=dict(stored.env),
                ),
                None,
            )
        if isinstance(stored, OAuthCredential):
            if stored.expired():
                return None, stored
            return (
                ResolvedAuth(
                    token=stored.access,
                    source=f"{provider_id} login",
                    extra=dict(stored.extra),
                ),
                None,
            )

        return self._from_env(provider_id), None

    def has_credential(self, provider_id: str) -> bool:
        """Whether a route is usable, without refreshing or exposing anything.

        Drives which providers ``/model`` is allowed to offer.
        """
        if self.store.read(provider_id) is not None:
            return True
        return any(os.environ.get(name) for name in PROVIDER_ENV.get(provider_id, ()))

    def source(self, provider_id: str) -> str | None:
        """Where the active credential comes from, or ``None`` when there is none.

        The origin only - never the secret, and never a description of it, so
        callers can print this next to whatever label they choose.
        """
        if provider_id in ENV_FIRST:
            for name in PROVIDER_ENV.get(provider_id, ()):
                if os.environ.get(name):
                    return f"environment ({name})"

        if self.store.read(provider_id) is not None:
            return str(self.store.path)

        for name in PROVIDER_ENV.get(provider_id, ()):
            if os.environ.get(name):
                return f"environment ({name})"
        return None

    def shadowed_by_env(self, provider_id: str) -> str | None:
        """The env var that would override a credential saved right now.

        ``/configure`` uses this to explain why a freshly saved key had no
        effect, rather than leaving the user to guess.
        """
        if provider_id not in ENV_FIRST:
            return None
        return next((n for n in PROVIDER_ENV.get(provider_id, ()) if os.environ.get(n)), None)

    def _from_env(self, provider_id: str) -> ResolvedAuth | None:
        for name in PROVIDER_ENV.get(provider_id, ()):
            value = os.environ.get(name)
            if value:
                return ResolvedAuth(token=value, source=f"environment ({name})")
        return None

    async def _refresh(self, provider_id: str, stored: OAuthCredential) -> OAuthCredential:
        refresher = REFRESHERS.get(provider_id)
        if refresher is None:
            raise ExpiredCredential(
                provider_id,
                f"The {provider_id} login has expired and cannot be renewed. "
                f"Run `hx auth login {provider_id}`.",
            )

        async def renew(current: Credential | None) -> Credential | None:
            # Another turn may have refreshed while we waited for the lock;
            # spending our stale refresh token then would revoke the good one.
            if isinstance(current, OAuthCredential) and not current.expired():
                return None
            source = current if isinstance(current, OAuthCredential) else stored
            return await refresher(source)

        try:
            updated = await self.store.modify(provider_id, renew)
        except Exception as exc:
            raise ExpiredCredential(
                provider_id,
                f"Could not renew the {provider_id} login ({exc}). "
                f"Run `hx auth login {provider_id}`.",
            ) from exc

        if not isinstance(updated, OAuthCredential):
            raise ExpiredCredential(
                provider_id,
                f"The {provider_id} login is no longer valid. Run `hx auth login {provider_id}`.",
            )
        return updated


def missing_message(provider_id: str) -> str:
    """What to tell the user, and what to run, when a route has no credential."""
    if provider_id == OPENROUTER:
        return (
            "No OpenRouter API key found. Set OPENROUTER_API_KEY, or run `hx` interactively "
            "to save one. Get a key at https://openrouter.ai/keys"
        )
    if provider_id == codex_oauth.PROVIDER_ID:
        return (
            "Not signed in to ChatGPT. Run `hx auth login openai-codex`, or `/login` "
            "inside HX. Requires a ChatGPT Plus or Pro subscription."
        )
    if provider_id == TAVILY:
        return (
            "No Tavily API key found, so WebSearch and WebFetch are off. Run "
            "`hx auth set tavily` or set TAVILY_API_KEY. The free tier at "
            "https://app.tavily.com is 1000 searches a month."
        )
    return f"No credential for {provider_id}. Run `hx auth login {provider_id}`."
