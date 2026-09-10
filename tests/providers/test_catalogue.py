"""Catalogue loading: what survives a refresh that cannot reach the network."""

from __future__ import annotations

import json
import ssl
from pathlib import Path
from typing import Any

import httpx
import pytest

from hx.providers.models import ModelRegistry


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
        "openai-codex/gpt-5.3-codex",
        "openai-codex/gpt-5.3-codex-spark",
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
    assert {m.id for m in ModelRegistry().all()} == {
        "openai-codex/gpt-5.3-codex",
        "openai-codex/gpt-5.3-codex-spark",
    }
