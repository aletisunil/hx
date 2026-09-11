"""Codex transport: the headers the backend requires, and token freshness."""

from __future__ import annotations

import pytest

from hx.auth.resolve import ResolvedAuth
from hx.providers.base import ProviderError
from hx.providers.codex import CodexProvider, wire_model


def provider(tokens: list[ResolvedAuth], **kwargs: object) -> CodexProvider:
    """A provider whose resolver hands out ``tokens`` in order, last one sticking."""
    calls = iter(tokens)
    latest = tokens[-1]

    async def resolve() -> ResolvedAuth:
        nonlocal latest
        latest = next(calls, latest)
        return latest

    return CodexProvider(resolve, **kwargs)  # type: ignore[arg-type]


def auth(token: str, account: str | None = "acct-1") -> ResolvedAuth:
    extra = {"account_id": account} if account else {}
    return ResolvedAuth(token=token, source="test", extra=extra)


async def test_the_required_backend_headers_are_present() -> None:
    codex = provider([auth("at-1")], session_id="sess-9")
    headers = await codex._headers()

    assert headers["Authorization"] == "Bearer at-1"
    # The backend routes on this; without it every request is a 401.
    assert headers["chatgpt-account-id"] == "acct-1"
    assert headers["originator"] == "hx"
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["Accept"] == "text/event-stream"
    assert headers["session-id"] == "sess-9"
    assert headers["x-client-request-id"] == "sess-9"


async def test_no_session_id_means_no_session_headers() -> None:
    headers = await provider([auth("at-1")])._headers()
    assert "session-id" not in headers


async def test_a_login_without_an_account_id_fails_before_the_request() -> None:
    """Better here, with the fix in the message, than as an opaque 401."""
    codex = provider([auth("at-1", account=None)])
    with pytest.raises(ProviderError, match="hx auth login openai-codex"):
        await codex._headers()


async def test_a_refreshed_token_reaches_the_next_request() -> None:
    """Headers are built per request precisely so this works mid-session."""
    codex = provider([auth("stale"), auth("fresh")])
    assert (await codex._headers())["Authorization"] == "Bearer stale"
    assert (await codex._headers())["Authorization"] == "Bearer fresh"


def test_the_provider_namespace_is_stripped_before_it_goes_on_the_wire() -> None:
    assert wire_model("openai-codex/gpt-5.3-codex") == "gpt-5.3-codex"
    assert wire_model("gpt-5.3-codex") == "gpt-5.3-codex"


def test_a_rejected_credential_says_how_to_fix_it() -> None:
    from hx.providers.codex import _error_message

    assert "hx auth login openai-codex" in _error_message(401, "{}")
    assert "hx auth login openai-codex" in _error_message(403, "{}")
    assert "quota" in _error_message(400, '{"error": {"message": "quota exceeded"}}')


def test_the_session_id_follows_a_new_session() -> None:
    """It is the prompt-cache key, so a stale one would keep hitting the old cache."""
    codex = provider([auth("at-1")], session_id="old")
    codex.set_session_id("new")
    assert codex.session_id == "new"


def test_a_model_this_account_cannot_run_says_what_to_do_about_it() -> None:
    """The backend phrases it as though the model id were wrong. It is not:
    entitlement is per model and per account, and the account's own list is
    something HX can go and ask for."""
    from hx.providers.codex import _error_message

    body = '{"error": {"message": "The model `gpt-5.3-codex` is not supported when using Codex with a ChatGPT account."}}'
    message = _error_message(400, body)

    assert "not entitled" in message
    assert "/models refresh" in message


async def test_the_effort_is_asked_for_per_model_not_fixed_per_session() -> None:
    """The model can change without the route changing, and two models differ
    on what they will accept."""
    from hx.providers.codex import DEFAULT_EFFORT

    asked: list[str] = []

    def effort(model_id: str) -> str | None:
        asked.append(model_id)
        return {"openai-codex/deep": "xhigh"}.get(model_id)

    codex = provider([auth("at-1")], effort=effort)
    assert codex.effort("openai-codex/deep") == "xhigh"
    assert codex.effort("openai-codex/shallow") is None
    assert asked == ["openai-codex/deep", "openai-codex/shallow"]

    # Nothing wired in: one depth every Codex model accepts, as before.
    assert provider([auth("at-1")]).effort("openai-codex/anything") == DEFAULT_EFFORT
