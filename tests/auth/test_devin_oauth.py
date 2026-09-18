"""Devin sign-in: the authorize URL, the token exchange, and what gets stored."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from hx.auth.oauth import devin
from hx.auth.oauth.browser import OAuthError
from hx.auth.oauth.pkce import generate_pkce


def jwt(claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_the_authorize_url_is_the_one_the_devin_cli_opens() -> None:
    pkce = generate_pkce()
    url = urlparse(devin.authorize_url(pkce, "state-1"))
    query = parse_qs(url.query)

    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://app.devin.ai/auth/cli/continue"
    # Matched as a string by the server: 127.0.0.1, not localhost.
    assert query["redirect_uri"] == ["http://127.0.0.1:59653/callback"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge"] == [pkce.challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["state-1"]
    assert query["prompt"] == ["select_account"]
    assert "client_id" not in query
    assert pkce.verifier not in devin.authorize_url(pkce, "state-1")


class TokenEndpoint:
    """A scripted ``api.devin.ai/auth/cli/token``."""

    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return self.responses.pop(0)


@pytest.fixture()
def token_endpoint(monkeypatch: pytest.MonkeyPatch) -> TokenEndpoint:
    endpoint = TokenEndpoint()

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(endpoint.handler))

    monkeypatch.setattr(devin, "async_client", client)
    return endpoint


async def test_the_code_is_exchanged_as_json_with_its_verifier(
    token_endpoint: TokenEndpoint,
) -> None:
    token = jwt({"exp": 1_900_000_000, "email": "dev@example.com"})
    token_endpoint.responses.append(httpx.Response(200, json={"token": token}))

    credential = await devin.exchange("the-code", "the-verifier")

    [request] = token_endpoint.sent
    assert str(request.url) == "https://api.devin.ai/auth/cli/token"
    assert json.loads(request.content) == {"code": "the-code", "code_verifier": "the-verifier"}
    assert credential.access == token
    assert credential.expires == 1_900_000_000
    assert devin.account_of(credential) == "dev@example.com"


async def test_a_refused_code_reports_the_servers_reason(
    token_endpoint: TokenEndpoint,
) -> None:
    token_endpoint.responses.append(
        httpx.Response(401, json={"detail": "Invalid or expired code."})
    )
    with pytest.raises(OAuthError, match="Invalid or expired code"):
        await devin.exchange("stale", "v")


async def test_a_response_without_a_token_fails_the_sign_in(
    token_endpoint: TokenEndpoint,
) -> None:
    token_endpoint.responses.append(httpx.Response(200, json={}))
    with pytest.raises(OAuthError, match="no session token"):
        await devin.exchange("code", "v")


def test_an_opaque_session_token_is_kept_for_a_year() -> None:
    """Not every token is a JWT; one that cannot say when it expires still works."""
    credential = devin.credential_from("opaque-token")
    assert credential.expires == pytest.approx(devin.expiry_of("x", now=0) + _now(), abs=5)
    assert credential.refresh == "opaque-token"
    assert devin.account_of(credential) is None


def test_the_expiry_is_read_through_the_wire_prefix() -> None:
    token = jwt({"exp": 2_000_000_000})
    assert devin.expiry_of(f"devin-session-token${token}") == 2_000_000_000


def test_devin_has_no_refresher_so_expiry_means_signing_in_again() -> None:
    from hx.auth.resolve import REFRESHERS

    assert devin.PROVIDER_ID not in REFRESHERS


def _now() -> float:
    import time

    return time.time()
