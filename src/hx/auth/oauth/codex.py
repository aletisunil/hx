"""ChatGPT Plus/Pro sign-in for the Codex backend.

OpenAI endorses subscription use by third-party harnesses
(https://developers.openai.com/community/codex-for-oss), so this is the
supported path rather than a workaround: a normal PKCE authorization-code flow
against ``auth.openai.com``, with a device-code variant for machines that have
no browser.

The account id the Codex backend requires is not returned by the token
endpoint - it is a claim inside the access token, so it is decoded here and
kept alongside the credential. The subscription plan rides along for the same
reason: OAuth succeeds for *any* ChatGPT account, including a free one that
cannot call a single Codex model, and the backend only says so on the first
turn - as an opaque "model is not supported" 400.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
import webbrowser
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from hx.auth.oauth.callback import CallbackError, CallbackResult, LoopbackCallback, parse_redirect
from hx.auth.oauth.pkce import PKCE, generate_pkce, random_state
from hx.auth.store import OAuthCredential
from hx.net import async_client

PROVIDER_ID = "openai-codex"

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH_BASE}/oauth/authorize"
TOKEN_URL = f"{AUTH_BASE}/oauth/token"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"
SCOPE = "openid profile email offline_access"

DEVICE_USER_CODE_URL = f"{AUTH_BASE}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{AUTH_BASE}/api/accounts/deviceauth/token"
DEVICE_VERIFICATION_URI = f"{AUTH_BASE}/codex/device"
DEVICE_REDIRECT_URI = f"{AUTH_BASE}/deviceauth/callback"
DEVICE_TIMEOUT_SECONDS = 15 * 60

#: The access token is a JWT; the account id lives under this namespaced claim.
JWT_CLAIM = "https://api.openai.com/auth"

FREE_PLAN = "free"
"""The one plan known to complete the OAuth flow and then have every Codex
model rejected. Read at sign-in rather than discovered on the first failed
turn."""

UNKNOWN_PLAN = "unknown"
"""A plan name the token did not carry. Treated as entitled: an unrecognised
new plan must not lock a paying user out of a route they can use."""

ORIGINATOR = "hx"
HTTP_TIMEOUT = 30.0


class OAuthError(Exception):
    pass


class LoginInteraction(Protocol):
    """How a login flow talks to whoever started it (CLI prompt or TUI modal)."""

    def show_url(self, url: str, instructions: str) -> None: ...

    def show_device_code(self, user_code: str, verification_uri: str) -> None: ...

    def progress(self, message: str) -> None: ...

    async def prompt_paste(self, message: str) -> str:
        """Return a pasted redirect URL or code.

        May never return - the browser callback usually wins the race - so
        implementations must tolerate cancellation.
        """
        ...


def authorize_url(pkce: PKCE, state: str, *, originator: str = ORIGINATOR) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": originator,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def account_id_from_token(access_token: str) -> str:
    """Pull ``chatgpt_account_id`` out of the access token.

    Raises:
        OAuthError: when the token is not a JWT or carries no account id. The
            Codex backend rejects requests without the header, so failing here
            beats failing on the first turn.
    """
    parts = access_token.split(".")
    if len(parts) != 3:
        raise OAuthError("Access token is not a JWT; cannot read the ChatGPT account id.")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, json.JSONDecodeError) as exc:
        raise OAuthError("Could not decode the access token payload.") from exc

    account_id = (claims.get(JWT_CLAIM) or {}).get("chatgpt_account_id")
    if not isinstance(account_id, str) or not account_id:
        raise OAuthError(
            "No ChatGPT account id in the token. Is this account on a Plus or Pro plan?"
        )
    return account_id


def plan_from_token(access_token: str) -> str:
    """The ChatGPT plan the token was minted for, lowercased.

    Returns :data:`UNKNOWN_PLAN` when the claim is absent or the token is not a
    readable JWT - never raises. The plan decides whether Codex models are
    usable, and a login is still worth keeping when only that answer is
    missing.
    """
    parts = access_token.split(".")
    if len(parts) != 3:
        return UNKNOWN_PLAN
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, json.JSONDecodeError):
        return UNKNOWN_PLAN
    plan = (claims.get(JWT_CLAIM) or {}).get("chatgpt_plan_type")
    return plan.lower() if isinstance(plan, str) and plan else UNKNOWN_PLAN


def plan_of(credential: OAuthCredential) -> str:
    """Cached on the credential; falls back to decoding the token.

    A credential saved before plans were recorded has no ``plan`` in ``extra``,
    so the token is read instead of reporting the login as unusable.
    """
    cached = credential.extra.get("plan")
    if isinstance(cached, str) and cached:
        return cached.lower()
    return plan_from_token(credential.access)


def is_entitled(plan: str) -> bool:
    """Whether ``plan`` can run Codex models.

    Only ``free`` is refused. An unrecognised plan counts as entitled: the list
    of paid tiers changes, and an allow-list would turn every new one into a
    lockout of a subscription that actually works.
    """
    return plan.lower() not in {FREE_PLAN, ""}


def account_id(credential: OAuthCredential) -> str:
    """Cached on the credential; falls back to decoding the token."""
    cached = credential.extra.get("account_id")
    if isinstance(cached, str) and cached:
        return cached
    return account_id_from_token(credential.access)


def _credential(token: dict[str, Any]) -> OAuthCredential:
    access = token.get("access_token")
    refresh = token.get("refresh_token")
    expires_in = token.get("expires_in")
    if not isinstance(access, str) or not isinstance(refresh, str):
        raise OAuthError("Token response was missing access_token or refresh_token.")
    lifetime = _positive_float(expires_in, default=3600.0)

    return OAuthCredential(
        access=access,
        refresh=refresh,
        expires=time.time() + lifetime,
        extra={
            "account_id": account_id_from_token(access),
            # Recorded alongside the account id and re-derived on every refresh
            # for the same reason: an upgrade from Free to Plus has to be
            # visible without signing in again.
            "plan": plan_from_token(access),
        },
    )


async def _post_form(client: httpx.AsyncClient, url: str, form: dict[str, str]) -> dict[str, Any]:
    response = await client.post(
        url,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code >= 400:
        raise OAuthError(f"{url} returned {response.status_code}: {response.text[:400]}")
    return dict(response.json())


async def _exchange(code: str, verifier: str, redirect_uri: str) -> OAuthCredential:
    async with async_client(timeout=HTTP_TIMEOUT) as client:
        token = await _post_form(
            client,
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
        )
    return _credential(token)


async def refresh(credential: OAuthCredential) -> OAuthCredential:
    """Swap the refresh token for a new pair.

    The account id is re-derived from the new access token rather than carried
    over - a refresh after an account switch would otherwise keep addressing
    the old workspace.
    """
    async with async_client(timeout=HTTP_TIMEOUT) as client:
        token = await _post_form(
            client,
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": credential.refresh,
            },
        )
    return _credential(token)


async def login_browser(interaction: LoginInteraction) -> OAuthCredential:
    """PKCE flow through the local browser, with a paste fallback.

    The callback server and the paste prompt race each other: over SSH the
    browser opens on the wrong machine and can never reach the loopback port,
    so pasting the final redirect URL has to work just as well.
    """
    pkce = generate_pkce()
    state = random_state()
    url = authorize_url(pkce, state)

    callback = LoopbackCallback(CALLBACK_PORT, CALLBACK_PATH, state=state)
    callback.start()
    try:
        interaction.show_url(
            url,
            "Complete the sign-in in your browser. On a remote machine, paste the "
            "final redirect URL here instead.",
        )
        if not callback.listening:
            interaction.progress(
                f"Port {CALLBACK_PORT} is busy, so the browser cannot hand the code back. "
                "Paste the redirect URL here instead."
            )
        elif not await _open_browser(url):
            interaction.progress("Could not open a browser - open the URL above manually.")

        result = await _race_callback_and_paste(callback, interaction)
    finally:
        try:
            await callback.aclose()
        except asyncio.CancelledError:
            # Cancelled mid-teardown: finish the job on this thread rather than
            # leaving the port bound for the life of the process, which would
            # cost the *next* login its browser callback.
            callback.close()
            raise

    if result.state is not None and result.state != state:
        raise OAuthError("OAuth state mismatch - discard this login and try again.")

    interaction.progress("Exchanging the authorization code…")
    return await _exchange(result.code, pkce.verifier, REDIRECT_URI)


async def _race_callback_and_paste(
    callback: LoopbackCallback,
    interaction: LoginInteraction,
) -> CallbackResult:
    paste_task = asyncio.ensure_future(
        interaction.prompt_paste("Paste the redirect URL or authorization code:")
    )
    wait_task = asyncio.ensure_future(callback.wait())
    try:
        done, _ = await asyncio.wait({paste_task, wait_task}, return_when=asyncio.FIRST_COMPLETED)
        # Prefer the loopback result: it is the one whose state we validated.
        if wait_task in done and not wait_task.cancelled():
            exc = wait_task.exception()
            if exc is None:
                return wait_task.result()
            if paste_task not in done:
                raise exc
        if paste_task in done:
            return parse_redirect(paste_task.result())
        raise CallbackError("Login did not complete.")
    finally:
        for task in (paste_task, wait_task):
            if not task.done():
                task.cancel()


async def _open_browser(url: str) -> bool:
    """Open the URL without stalling the caller's event loop.

    ``webbrowser.open`` is not a quick handoff: on macOS it writes AppleScript
    to ``osascript`` and waits for it, which takes as long as the browser takes
    to come up. On the event loop that freezes the whole TUI - including the
    Escape that cancels the login and the field the user is meant to paste into.
    """
    try:
        return await asyncio.to_thread(webbrowser.open, url)
    except webbrowser.Error:
        return False


async def login_device_code(interaction: LoginInteraction) -> OAuthCredential:
    """Headless flow: show a short code, poll until the user enters it elsewhere."""
    async with async_client(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(DEVICE_USER_CODE_URL, json={"client_id": CLIENT_ID})
        if response.status_code == 404:
            raise OAuthError(
                "Device-code login is not enabled for this account. Use browser login."
            )
        if response.status_code >= 400:
            raise OAuthError(
                f"Device code request failed ({response.status_code}): {response.text[:400]}"
            )
        payload = response.json()

        device_id = payload.get("device_auth_id")
        user_code = payload.get("user_code")
        if not device_id or not user_code:
            raise OAuthError("Device code response was missing device_auth_id or user_code.")
        interval = _positive_float(payload.get("interval"), default=5.0)

        interaction.show_device_code(str(user_code), DEVICE_VERIFICATION_URI)

        deadline = time.monotonic() + DEVICE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            poll = await client.post(
                DEVICE_TOKEN_URL,
                json={"device_auth_id": device_id, "user_code": user_code},
            )
            if poll.status_code < 400:
                data = poll.json()
                code = data.get("authorization_code")
                verifier = data.get("code_verifier")
                if not code or not verifier:
                    raise OAuthError("Device auth completed without an authorization code.")
                interaction.progress("Exchanging the authorization code…")
                return await _exchange(code, verifier, DEVICE_REDIRECT_URI)

            status = _device_error(poll)
            if status == "slow_down":
                interval += 2.0
            elif status != "pending":
                raise OAuthError(f"Device auth failed ({poll.status_code}): {poll.text[:400]}")

    raise OAuthError("Device code expired before it was approved.")


def _device_error(response: httpx.Response) -> str:
    """Classify a non-2xx poll as pending, slow_down, or fatal."""
    if response.status_code in (403, 404):
        return "pending"
    try:
        error = response.json().get("error")
    except ValueError:
        return "fatal"
    code = error.get("code") if isinstance(error, dict) else error
    if code == "deviceauth_authorization_pending":
        return "pending"
    if code == "slow_down":
        return "slow_down"
    return "fatal"


def _positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
