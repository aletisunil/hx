"""The per-account Codex model list: what is asked for, and what is believed."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from hx.auth.resolve import ResolvedAuth
from hx.providers import codex_catalogue
from hx.providers.base import ProviderError
from hx.providers.codex_catalogue import (
    CLIENT_VERSION,
    FALLBACK_CONTEXT,
    fetch_models,
    parse_entry,
    parse_levels,
    resolve_effort,
)
from hx.providers.models import ModelInfo, ModelPricing


def auth(account: str | None = "acct-1") -> ResolvedAuth:
    return ResolvedAuth(
        token="at-1",
        source="test",
        extra={"account_id": account} if account else {},
    )


def entry(slug: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slug": slug,
        "display_name": slug.upper(),
        "context_window": 272_000,
        "max_context_window": 400_000,
        "visibility": "list",
        "supported_reasoning_levels": [
            {"effort": "low", "description": "quick"},
            {"effort": "high", "description": "slow"},
        ],
        "default_reasoning_level": "low",
    }
    base.update(overrides)
    return base


def stub(monkeypatch: pytest.MonkeyPatch, handler: Any, seen: list[httpx.Request] | None = None):
    """Point the catalogue's own client at ``handler``.

    Patched on the module rather than on :mod:`hx.net`: the name is imported at
    module scope, so rebinding the source has no effect on the caller.
    """

    def record(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("verify", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(codex_catalogue, "async_client", client)


async def test_the_account_and_client_version_decide_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both are what make the reply specific rather than generic.

    The account selects whose entitlements come back; the client version is
    what the backend filters each model's ``minimal_client_version`` against,
    and asking as HX's own version returns an empty list.
    """
    seen: list[httpx.Request] = []
    stub(
        monkeypatch, lambda _r: httpx.Response(200, json={"models": [entry("gpt-5.6-terra")]}), seen
    )

    models = await fetch_models(auth())

    assert [m.id for m in models] == ["openai-codex/gpt-5.6-terra"]
    assert seen[0].headers["chatgpt-account-id"] == "acct-1"
    assert seen[0].url.params["client_version"] == CLIENT_VERSION


async def test_only_the_models_meant_to_be_picked_are_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalogue also carries internal entries, which are not offers."""
    body = {
        "models": [
            entry("gpt-5.6-terra"),
            entry("gpt-5.6-codex-mini", visibility="internal"),
            # No slug: nothing to route a request to, whatever else it says.
            entry(""),
        ]
    }
    stub(monkeypatch, lambda _r: httpx.Response(200, json=body))

    assert [m.id for m in await fetch_models(auth())] == ["openai-codex/gpt-5.6-terra"]


async def test_a_login_with_no_account_id_is_refused_before_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub(monkeypatch, lambda _r: httpx.Response(200, json={"models": []}))

    with pytest.raises(ProviderError, match="hx auth login openai-codex"):
        await fetch_models(auth(account=None))


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_credential_names_the_way_back_in(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    stub(monkeypatch, lambda _r: httpx.Response(status, text="nope"))

    with pytest.raises(ProviderError, match="hx auth login openai-codex"):
        await fetch_models(auth())


async def test_a_reply_that_is_not_a_catalogue_raises_rather_than_emptying_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller keeps the models it had; an empty picker is the worse answer."""
    stub(monkeypatch, lambda _r: httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(ProviderError, match="not JSON"):
        await fetch_models(auth())

    stub(monkeypatch, lambda _r: httpx.Response(200, json={"data": []}))
    with pytest.raises(ProviderError, match="no model list"):
        await fetch_models(auth())


def test_an_entry_gauges_against_the_window_the_default_request_buys() -> None:
    """``max_context_window`` is a long-context tier the turn does not pay for."""
    info = parse_entry(entry("gpt-5.6-terra"))

    assert info.context_window == 272_000
    assert info.name == "GPT-5.6-TERRA"
    assert info.is_subscription and info.pricing == ModelPricing()
    assert info.reasoning_levels == ("low", "high")
    assert info.default_reasoning_level == "low"


def test_an_entry_with_no_usable_window_still_lists() -> None:
    """A model HX cannot size is still a model the account can call."""
    assert parse_entry(entry("x", context_window=None)).context_window == FALLBACK_CONTEXT
    assert parse_entry(entry("x", context_window="lots")).context_window == FALLBACK_CONTEXT


def test_levels_come_back_weakest_first_and_unknown_ones_are_dropped() -> None:
    """Where an unrecognised level sits on the scale is exactly what is unknown."""
    raw = [
        {"effort": "high"},
        {"effort": "none"},
        {"effort": "telepathic"},
        {"effort": "medium"},
        "nonsense",
    ]
    assert parse_levels(raw) == ("none", "medium", "high")
    assert parse_levels(None) == ()


def _model(levels: tuple[str, ...], default: str | None = None) -> ModelInfo:
    return ModelInfo(
        id="openai-codex/test",
        name="test",
        context_window=1000,
        max_output_tokens=100,
        pricing=ModelPricing(),
        reasoning_levels=levels,
        default_reasoning_level=default,
    )


def test_nothing_requested_leaves_the_model_at_its_own_default() -> None:
    assert resolve_effort(None, _model(("low", "high"), "low")) == "low"
    # No default published either: the backend's own choice beats one invented here.
    assert resolve_effort(None, _model(("low", "high"))) is None


def test_an_effort_the_model_does_not_offer_is_lowered_rather_than_refused() -> None:
    """Otherwise one setting becomes a list of models it cannot be used with."""
    assert resolve_effort("max", _model(("low", "medium", "high", "xhigh"))) == "xhigh"
    assert resolve_effort("high", _model(("low", "medium", "high"))) == "high"
    # Nothing weaker on offer: the weakest there is, rather than nothing.
    assert resolve_effort("none", _model(("medium", "high"))) == "medium"


def test_an_effort_off_the_known_scale_is_left_to_the_backend() -> None:
    """Silently running at some other depth would be worse than being refused."""
    assert resolve_effort("telepathic", _model(("low", "high"))) == "telepathic"
    # A model that publishes no levels is not a model HX can clamp against.
    assert resolve_effort("max", _model(())) == "max"
