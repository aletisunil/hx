"""Catalogue loading: what survives a refresh that cannot reach the network."""

from __future__ import annotations

import json
import ssl
from pathlib import Path
from typing import Any

import httpx
import pytest

from hx.providers.models import CODEX_MODELS, ModelRegistry


class _Resolver:
    """Just enough :class:`~hx.auth.resolve.AuthResolver` for the registry."""

    def has_credential(self, provider_id: str) -> bool:
        return provider_id == "openrouter"

    def resolve_static(self, provider_id: str) -> Any:
        return type("Resolved", (), {"token": "sk-test"})()


def _write_cache(hx_home: Path, model_id: str) -> None:
    (hx_home / "models.json").write_text(
        json.dumps(
            {
                "fetched_at": 1.0,
                "models": [{"id": model_id, "name": model_id, "context_length": 200_000}],
            }
        )
    )


def _tls_failure() -> httpx.ConnectError:
    error = httpx.ConnectError("connection failed")
    error.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed")
    error.request = httpx.Request("GET", "https://openrouter.ai/api/v1/models")
    return error


async def test_a_failed_refresh_keeps_the_cached_catalogue(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing the network must not also lose yesterday's models."""
    _write_cache(hx_home, "anthropic/claude-sonnet-4.5")
    registry = ModelRegistry()
    assert [m.id for m in registry.all()] == [
        "anthropic/claude-sonnet-4.5",
        *sorted(m.id for m in CODEX_MODELS),
    ]

    async def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise _tls_failure()

    monkeypatch.setattr("hx.providers.openrouter.fetch_models", boom)
    with pytest.raises(httpx.ConnectError):
        await registry.refresh(_Resolver())

    assert "anthropic/claude-sonnet-4.5" in {m.id for m in registry.all()}


async def test_a_failed_refresh_records_why(hx_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ModelRegistry()

    async def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise _tls_failure()

    monkeypatch.setattr("hx.providers.openrouter.fetch_models", boom)
    with pytest.raises(httpx.ConnectError):
        await registry.refresh(_Resolver())

    assert registry.refresh_error is not None
    assert "TLS certificate verification failed" in registry.refresh_error


async def test_a_successful_refresh_clears_the_recorded_failure(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ModelRegistry()
    registry.refresh_error = "stale complaint"

    async def catalogue(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": "openai/gpt-5", "name": "GPT-5", "context_length": 400_000}]

    monkeypatch.setattr("hx.providers.openrouter.fetch_models", catalogue)
    await registry.refresh(_Resolver())

    assert registry.refresh_error is None
    assert "openai/gpt-5" in {m.id for m in registry.all()}


def test_routes_with_no_catalogue_endpoint_survive_a_missing_cache(hx_home: Path) -> None:
    """A machine whose first fetch failed still has the static routes."""
    assert not (hx_home / "models.json").exists()
    assert {m.id for m in ModelRegistry().all()} == {m.id for m in CODEX_MODELS}


class _CodexResolver:
    """Signed in to Codex, and to nothing else."""

    def __init__(self, account: str = "acct-1") -> None:
        self.account = account

    def has_credential(self, provider_id: str) -> bool:
        return provider_id == "openai-codex"

    async def resolve(self, provider_id: str) -> Any:
        from hx.auth.resolve import ResolvedAuth

        return ResolvedAuth(token="at-1", source="test", extra={"account_id": self.account})


def _codex_info(slug: str, levels: tuple[str, ...] = ("low", "high")) -> Any:
    from hx.providers.models import CacheMode, ModelInfo, ModelPricing

    return ModelInfo(
        id=f"openai-codex/{slug}",
        name=slug,
        context_window=272_000,
        max_output_tokens=128_000,
        pricing=ModelPricing(),
        cache_mode=CacheMode.IMPLICIT,
        provider_id="openai-codex",
        is_subscription=True,
        reasoning_levels=levels,
        default_reasoning_level=levels[0],
    )


async def test_the_accounts_own_list_replaces_the_shipped_guess(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entitlement is per account, so the fallback is a guess about someone else."""

    async def catalogue(auth: Any) -> list[Any]:
        return [_codex_info("gpt-6-nova")]

    monkeypatch.setattr("hx.providers.codex_catalogue.fetch_models", catalogue)
    registry = ModelRegistry()
    await registry.refresh(_CodexResolver())

    codex = {m.id for m in registry.all() if m.provider_id == "openai-codex"}
    assert codex == {"openai-codex/gpt-6-nova"}
    assert registry.codex_error is None


async def test_an_empty_answer_is_treated_as_no_answer(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is what a client version the backend does not recognise returns, and
    an empty picker is worse than a stale one."""

    async def nothing(auth: Any) -> list[Any]:
        return []

    monkeypatch.setattr("hx.providers.codex_catalogue.fetch_models", nothing)
    registry = ModelRegistry()
    await registry.refresh(_CodexResolver())

    assert {m.id for m in registry.all()} == {m.id for m in CODEX_MODELS}


async def test_a_codex_failure_is_recorded_without_costing_the_rest(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OpenRouter catalogue that did arrive has nothing to do with it."""
    from hx.providers.base import ProviderError

    class _Both(_CodexResolver):
        def has_credential(self, provider_id: str) -> bool:
            return True

        def resolve_static(self, provider_id: str) -> Any:
            return type("Resolved", (), {"token": "sk-test"})()

    async def boom(auth: Any) -> list[Any]:
        raise ProviderError("catalogue is down")

    async def catalogue(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": "openai/gpt-5", "name": "GPT-5", "context_length": 400_000}]

    monkeypatch.setattr("hx.providers.codex_catalogue.fetch_models", boom)
    monkeypatch.setattr("hx.providers.openrouter.fetch_models", catalogue)
    registry = ModelRegistry()
    await registry.refresh(_Both())

    assert registry.refresh_error is None
    assert registry.codex_error is not None and "down" in registry.codex_error
    assert "openai/gpt-5" in {m.id for m in registry.all()}
    # The shipped list still offers models to try.
    assert {m.id for m in CODEX_MODELS} <= {m.id for m in registry.all()}


async def test_the_fetched_list_survives_a_restart(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this, every start shows the fallback until something refreshes."""

    async def catalogue(auth: Any) -> list[Any]:
        return [_codex_info("gpt-6-nova", levels=("low", "medium", "high"))]

    monkeypatch.setattr("hx.providers.codex_catalogue.fetch_models", catalogue)
    await ModelRegistry().refresh(_CodexResolver())

    restored = ModelRegistry()
    restored.load_cache()
    info = restored.get_or_default("openai-codex/gpt-6-nova")
    assert info.is_subscription and info.context_window == 272_000
    # The levels ride along, or /effort would have nothing to offer offline.
    assert info.reasoning_levels == ("low", "medium", "high")
    assert info.default_reasoning_level == "low"


def test_settings_can_name_a_model_the_catalogue_does_not_advertise(hx_home: Path) -> None:
    registry = ModelRegistry()
    added = registry.add_codex_models(["gpt-6-secret", "openai-codex/gpt-5.5", " "])

    # Bare or namespaced, and never a duplicate of one already known.
    assert added == ["openai-codex/gpt-6-secret"]
    registry.load_cache()
    info = registry.get_or_default("openai-codex/gpt-6-secret")
    assert info.is_subscription and info.provider_id == "openai-codex"


def test_an_effort_is_clamped_per_model_not_per_session(hx_home: Path) -> None:
    """The strongest effort one model offers is off the scale on another."""
    registry = ModelRegistry()
    registry.load_cache()
    registry.set_reasoning_effort("ultra")

    deepest = max(CODEX_MODELS, key=lambda m: len(m.reasoning_levels))
    shallowest = min(CODEX_MODELS, key=lambda m: len(m.reasoning_levels))
    assert registry.reasoning_effort(deepest.id) == deepest.reasoning_levels[-1]
    assert registry.reasoning_effort(shallowest.id) == shallowest.reasoning_levels[-1]
    # A route that publishes no levels is not one HX can claim a depth for.
    assert registry.displayed_effort("anthropic/claude-sonnet-4.5") is None


async def test_a_settings_id_does_not_overwrite_what_the_backend_said(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settings entry carries guessed figures and no reasoning levels.

    Settings are read before the cache is, so an id named in both was added as
    an escape hatch and then laid over the real catalogue entry - costing that
    model its context window and its `/effort` levels.
    """

    async def catalogue(auth: Any) -> list[Any]:
        return [_codex_info("gpt-6-nova", levels=("low", "high"))]

    monkeypatch.setattr("hx.providers.codex_catalogue.fetch_models", catalogue)
    await ModelRegistry().refresh(_CodexResolver())

    registry = ModelRegistry()
    registry.add_codex_models(["gpt-6-nova"])
    registry.load_cache()

    info = registry.get_or_default("openai-codex/gpt-6-nova")
    assert info.reasoning_levels == ("low", "high")
    assert info.context_window == 272_000


async def test_another_accounts_entitlements_do_not_outlive_the_login(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entitlement is per account, so a list fetched for one login says nothing
    about the next one - least of all when the fetch for the new one failed."""
    from hx.providers.base import ProviderError

    async def catalogue(auth: Any) -> list[Any]:
        return [_codex_info("gpt-6-nova")]

    monkeypatch.setattr("hx.providers.codex_catalogue.fetch_models", catalogue)
    await ModelRegistry().refresh(_CodexResolver(account="acct-1"))

    async def boom(auth: Any) -> list[Any]:
        raise ProviderError("catalogue is down")

    monkeypatch.setattr("hx.providers.codex_catalogue.fetch_models", boom)
    registry = ModelRegistry()
    registry.load_cache()
    await registry.refresh(_CodexResolver(account="acct-2"))

    listed = {m.id for m in registry.all() if m.provider_id == "openai-codex"}
    assert "openai-codex/gpt-6-nova" not in listed
    assert listed == {m.id for m in CODEX_MODELS}
