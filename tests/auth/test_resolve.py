"""Credential resolution: precedence, refresh, and what happens when it fails."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from hx.auth.resolve import AuthResolver, ExpiredCredential, MissingCredential
from hx.auth.store import ApiKeyCredential, AuthStore, OAuthCredential


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HX_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_openrouter_keeps_env_over_file(hx_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HX has always behaved this way; changing it would silently switch keys."""
    AuthStore().save("openrouter", ApiKeyCredential(key="sk-or-file"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")

    resolved = AuthResolver().resolve_static("openrouter")
    assert resolved.token == "sk-or-env"
    assert resolved.source == "environment (OPENROUTER_API_KEY)"


def test_hx_prefixed_env_wins_over_the_bare_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bare")
    monkeypatch.setenv("HX_OPENROUTER_API_KEY", "sk-or-hx")
    assert AuthResolver().resolve_static("openrouter").token == "sk-or-hx"


def test_the_saved_key_is_used_when_no_variable_is_set(hx_home: Path) -> None:
    AuthStore().save("openrouter", ApiKeyCredential(key="sk-or-file"))
    assert AuthResolver().resolve_static("openrouter").token == "sk-or-file"


def test_a_missing_credential_says_what_to_run(hx_home: Path) -> None:
    with pytest.raises(MissingCredential, match=re.escape("openrouter.ai/keys")):
        AuthResolver().resolve_static("openrouter")
    with pytest.raises(MissingCredential, match="hx auth login openai-codex"):
        AuthResolver().resolve_static("openai-codex")


async def test_a_live_oauth_token_is_used_as_is(hx_home: Path) -> None:
    AuthStore().save(
        "openai-codex",
        OAuthCredential(
            access="live", refresh="r", expires=time.time() + 3600, extra={"account_id": "acct"}
        ),
    )
    resolved = await AuthResolver().resolve("openai-codex")
    assert resolved.token == "live"
    assert resolved.extra["account_id"] == "acct", "the account id must reach the provider"


async def test_an_expiring_token_is_refreshed_and_persisted(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AuthStore()
    store.save(
        "openai-codex",
        OAuthCredential(access="stale", refresh="r0", expires=time.time() + 10),
    )

    calls: list[str] = []

    async def fake_refresh(credential: OAuthCredential) -> OAuthCredential:
        calls.append(credential.refresh)
        return OAuthCredential(
            access="fresh", refresh="r1", expires=time.time() + 3600, extra={"account_id": "a"}
        )

    monkeypatch.setitem(
        __import__("hx.auth.resolve", fromlist=["REFRESHERS"]).REFRESHERS,
        "openai-codex",
        fake_refresh,
    )

    resolved = await AuthResolver(store).resolve("openai-codex")
    assert resolved.token == "fresh"
    assert calls == ["r0"]

    # Persisted, so the next process does not spend the same refresh token.
    saved = AuthStore().read("openai-codex")
    assert isinstance(saved, OAuthCredential)
    assert (saved.access, saved.refresh) == ("fresh", "r1")


async def test_a_failed_refresh_does_not_fall_back_to_the_environment(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silently switching to another credential hides why the login broke."""
    import hx.auth.resolve as resolve_module

    store = AuthStore()
    store.save("openai-codex", OAuthCredential(access="a", refresh="r", expires=0.0))
    monkeypatch.setitem(resolve_module.PROVIDER_ENV, "openai-codex", ("CODEX_FALLBACK",))
    monkeypatch.setenv("CODEX_FALLBACK", "should-not-be-used")

    async def boom(credential: OAuthCredential) -> OAuthCredential:
        raise RuntimeError("network down")

    monkeypatch.setitem(resolve_module.REFRESHERS, "openai-codex", boom)

    with pytest.raises(ExpiredCredential, match="hx auth login openai-codex"):
        await AuthResolver(store).resolve("openai-codex")


async def test_a_token_refreshed_by_another_turn_is_reused(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh runs under the lock, so it must re-check before spending."""
    import hx.auth.resolve as resolve_module

    store = AuthStore()
    store.save("openai-codex", OAuthCredential(access="stale", refresh="r0", expires=0.0))

    async def refresh(credential: OAuthCredential) -> OAuthCredential:
        return OAuthCredential(access="first", refresh="r1", expires=time.time() + 3600)

    monkeypatch.setitem(resolve_module.REFRESHERS, "openai-codex", refresh)
    resolver = AuthResolver(store)

    first = await resolver.resolve("openai-codex")

    async def must_not_run(credential: OAuthCredential) -> OAuthCredential:
        raise AssertionError("a valid token was refreshed again")

    monkeypatch.setitem(resolve_module.REFRESHERS, "openai-codex", must_not_run)
    second = await resolver.resolve("openai-codex")
    assert first.token == second.token == "first"


def test_resolve_static_refuses_to_guess_at_an_expired_login(hx_home: Path) -> None:
    """Synchronous callers cannot refresh, so they must not paper over it."""
    AuthStore().save("openai-codex", OAuthCredential(access="a", refresh="r", expires=0.0))
    with pytest.raises(ExpiredCredential):
        AuthResolver().resolve_static("openai-codex")


def test_has_credential_drives_what_the_picker_may_offer(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = AuthResolver()
    assert resolver.has_credential("openrouter") is False
    assert resolver.has_credential("openai-codex") is False

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    assert resolver.has_credential("openrouter") is True

    AuthStore().save("openai-codex", OAuthCredential(access="a", refresh="r", expires=0.0))
    assert resolver.has_credential("openai-codex") is True, "expired still counts as signed in"


def test_shadowed_by_env_only_applies_where_the_env_wins(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    resolver = AuthResolver()
    assert resolver.shadowed_by_env("openrouter") == "OPENROUTER_API_KEY"
    assert resolver.shadowed_by_env("openai-codex") is None
