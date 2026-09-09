"""The credential file: migration, permissions, and write serialisation."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from hx.auth.store import (
    ApiKeyCredential,
    AuthStore,
    Credential,
    OAuthCredential,
    mask,
)


def test_credentials_round_trip_with_restrictive_permissions(hx_home: Path) -> None:
    store = AuthStore()
    store.save("openrouter", ApiKeyCredential(key="sk-or-test"))
    store.save(
        "openai-codex",
        OAuthCredential(
            access="at", refresh="rt", expires=1_800_000_000.0, extra={"account_id": "acct-1"}
        ),
    )

    reloaded = AuthStore()
    api = reloaded.read("openrouter")
    oauth = reloaded.read("openai-codex")
    assert isinstance(api, ApiKeyCredential) and api.key == "sk-or-test"
    assert isinstance(oauth, OAuthCredential)
    assert (oauth.access, oauth.refresh) == ("at", "rt")
    assert oauth.extra["account_id"] == "acct-1", "provider fields must survive the round trip"

    assert (hx_home / "auth.json").stat().st_mode & 0o777 == 0o600


def test_the_legacy_flat_key_is_still_readable(hx_home: Path) -> None:
    """Upgrading must not lock a user out of the key they already saved."""
    (hx_home / "auth.json").write_text(json.dumps({"openrouter_api_key": "sk-or-legacy"}))

    credential = AuthStore().read("openrouter")
    assert isinstance(credential, ApiKeyCredential)
    assert credential.key == "sk-or-legacy"


def test_the_legacy_key_is_rewritten_on_the_next_save(hx_home: Path) -> None:
    path = hx_home / "auth.json"
    path.write_text(json.dumps({"openrouter_api_key": "sk-or-legacy"}))

    store = AuthStore()
    store.save("openai-codex", OAuthCredential(access="a", refresh="r", expires=0.0))

    written = json.loads(path.read_text())
    assert written["openrouter"] == {"type": "api_key", "key": "sk-or-legacy"}
    assert "openrouter_api_key" not in written, "the legacy shape should not linger"


def test_a_tagged_entry_wins_over_the_legacy_key(hx_home: Path) -> None:
    (hx_home / "auth.json").write_text(
        json.dumps(
            {
                "openrouter_api_key": "sk-or-stale",
                "openrouter": {"type": "api_key", "key": "sk-or-current"},
            }
        )
    )
    credential = AuthStore().read("openrouter")
    assert isinstance(credential, ApiKeyCredential)
    assert credential.key == "sk-or-current"


def test_one_corrupt_entry_does_not_hide_the_others(hx_home: Path) -> None:
    """A provider entry HX cannot parse must not cost the user every other login."""
    (hx_home / "auth.json").write_text(
        json.dumps(
            {
                "broken": {"type": "oauth"},
                "openrouter": {"type": "api_key", "key": "sk-or-fine"},
            }
        )
    )
    store = AuthStore()
    assert store.read("broken") is None
    assert store.read("openrouter") is not None


def test_unreadable_json_is_treated_as_empty(hx_home: Path) -> None:
    (hx_home / "auth.json").write_text("{not json")
    assert AuthStore().read("openrouter") is None
    assert AuthStore().list() == []


def test_delete_reports_whether_anything_went(hx_home: Path) -> None:
    store = AuthStore()
    store.save("openrouter", ApiKeyCredential(key="sk-or-test"))
    assert store.delete("openrouter") is True
    assert store.delete("openrouter") is False
    assert store.read("openrouter") is None


def test_list_reports_types_without_exposing_secrets(hx_home: Path) -> None:
    store = AuthStore()
    store.save("openrouter", ApiKeyCredential(key="sk-or-test"))
    store.save("openai-codex", OAuthCredential(access="a", refresh="r", expires=99.0))

    infos = store.list()
    assert [(i.provider_id, i.type) for i in infos] == [
        ("openai-codex", "oauth"),
        ("openrouter", "api_key"),
    ]
    assert "sk-or-test" not in repr(infos)


async def test_concurrent_modifies_are_serialised(hx_home: Path) -> None:
    """Two turns refreshing at once must not interleave read-modify-write.

    Without the per-provider lock both would read the same token and the second
    write would silently drop the first.
    """
    store = AuthStore()
    store.save("openai-codex", OAuthCredential(access="a0", refresh="r0", expires=0.0))
    order: list[str] = []

    def bump(tag: str):
        async def run(current: Credential | None) -> Credential | None:
            assert isinstance(current, OAuthCredential)
            order.append(f"read-{tag}")
            await asyncio.sleep(0)  # a real refresh awaits the network here
            order.append(f"write-{tag}")
            return OAuthCredential(access=current.access + tag, refresh="r0", expires=0.0)

        return run

    await asyncio.gather(
        store.modify("openai-codex", bump("A")), store.modify("openai-codex", bump("B"))
    )

    assert order in (
        ["read-A", "write-A", "read-B", "write-B"],
        ["read-B", "write-B", "read-A", "write-A"],
    ), f"interleaved: {order}"
    final = store.read("openai-codex")
    assert isinstance(final, OAuthCredential)
    assert final.access in ("a0AB", "a0BA"), "both writes must be visible"


def test_expiry_uses_leeway_so_a_token_never_dies_mid_stream() -> None:
    fresh = OAuthCredential(access="a", refresh="r", expires=time.time() + 3600)
    nearly = OAuthCredential(access="a", refresh="r", expires=time.time() + 60)
    assert fresh.expired() is False
    assert nearly.expired() is True, "inside the 5-minute leeway"


def test_masking_never_shows_the_middle() -> None:
    assert mask("sk-or-v1-0123456789abcdef") == "sk-or-…cdef"
    assert "0123456789" not in mask("sk-or-v1-0123456789abcdef")
    assert mask("short") == "…"
