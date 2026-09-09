"""Routing: which provider serves a model id."""

from __future__ import annotations

from pathlib import Path

import pytest

from hx.auth.resolve import AuthResolver, MissingCredential
from hx.auth.store import ApiKeyCredential, AuthStore, OAuthCredential
from hx.providers import registry


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HX_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("model_id", "provider_id", "bare"),
    [
        # Every id that worked before the registry existed still routes to OpenRouter.
        ("anthropic/claude-sonnet-4.5", "openrouter", "anthropic/claude-sonnet-4.5"),
        ("openai/gpt-5", "openrouter", "openai/gpt-5"),
        ("some-local-model", "openrouter", "some-local-model"),
        ("openai-codex/gpt-5.3-codex", "openai-codex", "gpt-5.3-codex"),
    ],
)
def test_the_model_id_alone_decides_the_route(model_id: str, provider_id: str, bare: str) -> None:
    spec, remainder = registry.split_model_id(model_id)
    assert spec.id == provider_id
    assert remainder == bare


def test_openai_prefixed_openrouter_ids_are_not_mistaken_for_codex() -> None:
    """``openai/`` and ``openai-codex/`` differ by four characters."""
    assert registry.provider_for("openai/gpt-5-codex").id == "openrouter"


def test_only_the_codex_route_is_a_subscription() -> None:
    assert registry.get("openai-codex").is_subscription is True
    assert registry.get("openrouter").is_subscription is False


def test_an_unknown_provider_is_named_not_swallowed() -> None:
    with pytest.raises(registry.UnknownProvider):
        registry.get("anthropic-oauth")


def test_available_lists_only_routes_with_a_credential(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = AuthResolver()
    assert registry.available(resolver) == []

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    assert [spec.id for spec in registry.available(resolver)] == ["openrouter"]

    AuthStore().save("openai-codex", OAuthCredential(access="a", refresh="r", expires=0.0))
    assert [spec.id for spec in registry.available(resolver)] == ["openrouter", "openai-codex"]


def test_building_a_route_without_a_credential_is_actionable(hx_home: Path) -> None:
    with pytest.raises(MissingCredential) as excinfo:
        registry.build_provider("openai-codex/gpt-5.3-codex", AuthResolver())
    assert excinfo.value.provider_id == "openai-codex"


def test_the_openrouter_route_bakes_its_key_in(hx_home: Path) -> None:
    AuthStore().save("openrouter", ApiKeyCredential(key="sk-or-built"))
    provider = registry.build_provider("anthropic/claude-sonnet-4.5", AuthResolver())
    assert provider.name == "openrouter"
    assert provider._client.headers["Authorization"] == "Bearer sk-or-built"


def test_the_codex_route_carries_the_session_id(hx_home: Path) -> None:
    """The session id is the prompt-cache key, so it has to reach the provider."""
    AuthStore().save("openai-codex", OAuthCredential(access="a", refresh="r", expires=0.0))
    provider = registry.build_provider(
        "openai-codex/gpt-5.3-codex", AuthResolver(), session_id="sess-1"
    )
    assert provider.name == "openai-codex"
    assert provider.session_id == "sess-1"
