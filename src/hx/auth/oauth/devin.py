"""Devin subscription sign-in.

The same browser sign-in the official ``devin`` CLI performs: a PKCE
authorization-code flow against ``app.devin.ai``, redeemed at
``api.devin.ai/auth/cli/token`` for a Devin session token. There is no client
id and no refresh token - the session token is long-lived, and when it does
expire the only way forward is signing in again.

Devin Enterprise signs in through the same flow: the browser page offers "Log
in with Devin for Enterprise", which asks for the company and hands off to its
identity provider. The token that comes back is an ordinary session token; what
differs is the API server the tenant is served from, which the backend names on
the first call (:func:`hx.providers.devin.request_user_jwt`).

The session token is not what chat requests carry. Each request trades it for a
short-lived user JWT first (:mod:`hx.providers.devin`), so what is stored here
is the one secret that can mint the rest.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx

from hx.auth.oauth.browser import LoginInteraction, OAuthError, authorize_in_browser
from hx.auth.oauth.pkce import PKCE, generate_pkce
from hx.auth.store import OAuthCredential
from hx.net import async_client

PROVIDER_ID = "devin"

AUTHORIZE_URL = "https://app.devin.ai/auth/cli/continue"
TOKEN_URL = "https://api.devin.ai/auth/cli/token"
CALLBACK_PORT = 59653
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}"
"""The loopback address the Devin CLI registers. ``127.0.0.1`` rather than
``localhost``: the redirect is matched as a string."""

DEFAULT_LIFETIME = 365 * 24 * 3600.0
"""For a session token whose expiry cannot be read. The CLI assumes a year."""

HTTP_TIMEOUT = 30.0

ENTERPRISE_HINT = (
    'For Devin Enterprise, choose "Log in with Devin for Enterprise" and enter your company.'
)


def authorize_url(pkce: PKCE, state: str) -> str:
    params = {
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
        "state": state,
        # Lets someone with a work and a personal account pick, instead of
        # silently signing in to whichever one the browser last used.
        "prompt": "select_account",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def claims_of(token: str) -> dict[str, Any]:
    """The payload of a JWT-shaped token, or ``{}`` when it is not one.

    Never raises: a session token that is opaque today is still a working
    credential, it just cannot say when it expires.
    """
    parts = token.removeprefix("devin-session-token$").split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def expiry_of(token: str, *, now: float | None = None) -> float:
    """Epoch seconds at which ``token`` stops working."""
    exp = claims_of(token).get("exp")
    if isinstance(exp, int | float) and exp > 0:
        return float(exp)
    return (time.time() if now is None else now) + DEFAULT_LIFETIME


def credential_from(token: str) -> OAuthCredential:
    """Store a session token.

    ``refresh`` holds the token itself, not a refresh token: there is none, and
    the store needs the field. ``AuthResolver`` has no refresher registered for
    this route, so an expired login asks for a new sign-in rather than
    pretending it can be renewed.
    """
    extra: dict[str, Any] = {}
    claims = claims_of(token)
    for key in ("email", "name"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            extra[key] = value
    return OAuthCredential(access=token, refresh=token, expires=expiry_of(token), extra=extra)


async def exchange(code: str, verifier: str) -> OAuthCredential:
    """Redeem an authorization code for a session token."""
    async with async_client(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(
            TOKEN_URL,
            json={"code": code, "code_verifier": verifier},
            headers={"Accept": "application/json"},
        )
    if response.status_code >= 400:
        raise OAuthError(f"Devin refused the sign-in ({response.status_code}): {_detail(response)}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthError("Devin's token endpoint did not answer with JSON.") from exc
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise OAuthError("Devin's token response carried no session token.")
    return credential_from(token)


async def login_browser(interaction: LoginInteraction) -> OAuthCredential:
    """PKCE flow through the local browser, with a paste fallback."""
    pkce = generate_pkce()
    state = str(uuid.uuid4())
    result = await authorize_in_browser(
        authorize_url(pkce, state),
        port=CALLBACK_PORT,
        path=CALLBACK_PATH,
        state=state,
        interaction=interaction,
        instructions=ENTERPRISE_HINT,
    )
    interaction.progress("Exchanging the authorization code…")
    return await exchange(result.code, pkce.verifier)


def account_of(credential: OAuthCredential) -> str | None:
    """Who the login belongs to, for ``hx auth`` - never the token."""
    for key in ("email", "name"):
        value = credential.extra.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _detail(response: httpx.Response) -> str:
    """The reason in an error body - ``{"detail": ...}`` - or a short excerpt."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:400]
    if isinstance(payload, dict):
        for key in ("detail", "message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return response.text[:400]
