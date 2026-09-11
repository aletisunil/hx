"""The Codex OAuth flow's pure parts: URL construction and token decoding."""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

from hx.auth.oauth import codex
from hx.auth.oauth.callback import CallbackError, parse_redirect
from hx.auth.oauth.pkce import generate_pkce


def jwt(claims: dict[str, object]) -> str:
    """A token shaped like the real one; only the payload segment is read."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_the_authorize_url_carries_pkce_and_the_codex_flow_flags() -> None:
    pkce = generate_pkce()
    query = parse_qs(urlparse(codex.authorize_url(pkce, "state-1")).query)

    assert query["client_id"] == [codex.CLIENT_ID]
    assert query["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert query["code_challenge"] == [pkce.challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["state-1"]
    assert query["scope"] == ["openid profile email offline_access"]
    # Without these the account has no Codex entitlement on the issued token.
    assert query["codex_cli_simplified_flow"] == ["true"]
    assert query["id_token_add_organizations"] == ["true"]
    assert query["originator"] == ["hx"]


def test_the_verifier_never_appears_in_the_authorize_url() -> None:
    """Sending it would defeat the point of the challenge."""
    pkce = generate_pkce()
    assert pkce.verifier not in codex.authorize_url(pkce, "state-1")


def test_the_account_id_comes_out_of_the_token_claim() -> None:
    token = jwt({codex.JWT_CLAIM: {"chatgpt_account_id": "acct-42"}})
    assert codex.account_id_from_token(token) == "acct-42"


def test_a_token_without_an_account_id_is_rejected_with_a_reason() -> None:
    with pytest.raises(codex.OAuthError, match="Plus or Pro"):
        codex.account_id_from_token(jwt({codex.JWT_CLAIM: {}}))


@pytest.mark.parametrize("token", ["not-a-jwt", "a.b", "a.!!!.c"])
def test_a_malformed_token_fails_at_login_not_on_the_first_turn(token: str) -> None:
    with pytest.raises(codex.OAuthError):
        codex.account_id_from_token(token)


def test_a_stored_account_id_is_preferred_over_decoding() -> None:
    from hx.auth.store import OAuthCredential

    credential = OAuthCredential(
        access="opaque", refresh="r", expires=0.0, extra={"account_id": "acct-cached"}
    )
    assert codex.account_id(credential) == "acct-cached"


def test_the_plan_is_read_off_the_token_at_sign_in() -> None:
    """OAuth succeeds for any ChatGPT account, including a free one that cannot
    call a single Codex model - and the backend only says so on the first turn,
    as an opaque "model is not supported" 400."""
    assert codex.plan_from_token(jwt({codex.JWT_CLAIM: {"chatgpt_plan_type": "Plus"}})) == "plus"
    assert codex.plan_from_token(jwt({codex.JWT_CLAIM: {"chatgpt_plan_type": "free"}})) == "free"


@pytest.mark.parametrize("token", ["not-a-jwt", "a.b", "a.!!!.c", "a.e30.c"])
def test_an_unreadable_plan_is_unknown_rather_than_fatal(token: str) -> None:
    """A login is worth keeping when only that one answer is missing."""
    assert codex.plan_from_token(token) == codex.UNKNOWN_PLAN


def test_only_free_is_treated_as_unentitled() -> None:
    """An allow-list of paid tiers would turn every new plan into a lockout of
    a subscription that actually works."""
    assert not codex.is_entitled(codex.FREE_PLAN)
    assert codex.is_entitled("plus")
    assert codex.is_entitled(codex.UNKNOWN_PLAN)
    assert codex.is_entitled("some_tier_shipped_next_year")


def test_a_credential_saved_before_plans_were_recorded_still_reports_one() -> None:
    from hx.auth.store import OAuthCredential

    token = jwt({codex.JWT_CLAIM: {"chatgpt_plan_type": "Pro"}})
    stored = OAuthCredential(access=token, refresh="r", expires=0.0, extra={})
    assert codex.plan_of(stored) == "pro"

    cached = OAuthCredential(access=token, refresh="r", expires=0.0, extra={"plan": "Plus"})
    assert codex.plan_of(cached) == "plus"


@pytest.mark.parametrize(
    ("pasted", "code", "state"),
    [
        ("http://localhost:1455/auth/callback?code=abc&state=xyz", "abc", "xyz"),
        ("?code=abc&state=xyz", "abc", "xyz"),
        ("abc#xyz", "abc", "xyz"),
        ("  abc  ", "abc", None),
    ],
)
def test_the_paste_field_accepts_what_a_user_would_actually_paste(
    pasted: str, code: str, state: str | None
) -> None:
    """Over SSH this is the only way through, so it must not be fussy."""
    result = parse_redirect(pasted)
    assert (result.code, result.state) == (code, state)


def test_pasting_the_wrong_thing_says_so() -> None:
    with pytest.raises(CallbackError):
        parse_redirect("   ")
    with pytest.raises(CallbackError, match=re.escape("no ?code=")):
        parse_redirect("https://auth.openai.com/oauth/authorize?error=denied")


def test_a_busy_port_leaves_the_paste_path_open() -> None:
    """A stale listener on 1455 must not make signing in impossible."""
    import socket

    from hx.auth.oauth.callback import LoopbackCallback, callback_host

    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind((callback_host(), 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        with LoopbackCallback(port, "/auth/callback", state="s") as callback:
            assert callback.listening is False
    finally:
        blocker.close()


async def test_leaving_the_flow_unblocks_the_waiting_callback() -> None:
    """`asyncio.to_thread` cannot be cancelled, so exit has to wake the waiter.

    Without that, the pool thread stays parked on the event for the life of the
    process and the login can never be retried on the same port.
    """
    import asyncio

    from hx.auth.oauth.callback import CallbackError, LoopbackCallback

    with LoopbackCallback(0, "/auth/callback", state="s") as callback:
        assert callback.listening is True
        waiting = asyncio.ensure_future(callback.wait())
        await asyncio.sleep(0.05)
        assert not waiting.done(), "nothing has redirected yet"

    # Resolves on teardown rather than hanging until the timeout.
    with pytest.raises(CallbackError, match="cancelled"):
        await asyncio.wait_for(waiting, timeout=2)


async def test_cancelling_a_login_releases_the_callback_port() -> None:
    """A leaked port costs the next attempt its browser callback, silently:
    the flow still works by pasting, which is not what the user asked for."""
    import asyncio
    import contextlib
    import socket

    from hx.auth.oauth import codex

    class Silent:
        def show_url(self, url: str, instructions: str) -> None: ...
        def show_device_code(self, user_code: str, verification_uri: str) -> None: ...
        def progress(self, message: str) -> None: ...

        async def prompt_paste(self, message: str) -> str:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def opened(url: str) -> bool:
        return True

    original = codex._open_browser
    codex._open_browser = opened  # type: ignore[assignment]
    try:
        flow = asyncio.create_task(codex.login_browser(Silent()))
        await asyncio.sleep(0.1)
        flow.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await flow
    finally:
        codex._open_browser = original  # type: ignore[assignment]

    probe = socket.socket()
    try:
        from hx.auth.oauth.callback import callback_host

        probe.bind((callback_host(), codex.CALLBACK_PORT))
    finally:
        probe.close()
