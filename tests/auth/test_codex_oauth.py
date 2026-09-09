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
