"""The credential file: ``~/.hx/auth.json``.

One type-tagged credential per provider id::

    {
      "openrouter":   {"type": "api_key", "key": "sk-or-..."},
      "openai-codex": {"type": "oauth", "access": "...", "refresh": "...",
                       "expires": 1757000000.0, "account_id": "..."}
    }

Earlier versions stored a bare ``openrouter_api_key`` at the top level. That
shape is still read (see :func:`_migrate`) and is rewritten into the tagged
form on the next write, so upgrading needs no user action.

Every write goes through :meth:`AuthStore.modify`, which serialises per
provider id. A token refresh that raced a second turn would otherwise burn one
of the two refresh tokens and leave the stored one dead.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from hx.paths import auth_file

LEGACY_OPENROUTER_KEY = "openrouter_api_key"
OPENROUTER = "openrouter"


@dataclass(frozen=True, slots=True)
class ApiKeyCredential:
    key: str
    env: dict[str, str] = field(default_factory=dict)
    type: Literal["api_key"] = "api_key"

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": "api_key", "key": self.key}
        if self.env:
            data["env"] = dict(self.env)
        return data


@dataclass(frozen=True, slots=True)
class OAuthCredential:
    """``expires`` is epoch seconds, not the milliseconds other harnesses use."""

    access: str
    refresh: str
    expires: float
    extra: dict[str, Any] = field(default_factory=dict)
    type: Literal["oauth"] = "oauth"

    def expired(self, *, leeway: float = 300.0) -> bool:
        """True once the token is inside ``leeway`` seconds of expiry.

        Refreshing early is cheap; a token that expires mid-stream is not.
        """
        return time.time() >= (self.expires - leeway)

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "oauth",
            "access": self.access,
            "refresh": self.refresh,
            "expires": self.expires,
            **self.extra,
        }


Credential = ApiKeyCredential | OAuthCredential


@dataclass(frozen=True, slots=True)
class CredentialInfo:
    """What a credential is, without the secret. For ``/login`` and status output."""

    provider_id: str
    type: Literal["api_key", "oauth"]
    expires: float | None = None


class AuthStore:
    """Reads and writes the credential file.

    Reads hit the disk every time rather than caching: ``hx auth login`` in one
    terminal must be visible to a session already running in another.
    """

    _RESERVED = frozenset({LEGACY_OPENROUTER_KEY})

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def path(self) -> Path:
        # Resolved late so a test that monkeypatches $HX_HOME after construction
        # still gets the isolated file.
        return self._path if self._path is not None else auth_file()

    def read(self, provider_id: str) -> Credential | None:
        return self._load().get(provider_id)

    def list(self) -> list[CredentialInfo]:
        infos = [
            CredentialInfo(
                provider_id=pid,
                type=cred.type,
                expires=cred.expires if isinstance(cred, OAuthCredential) else None,
            )
            for pid, cred in self._load().items()
        ]
        return sorted(infos, key=lambda info: info.provider_id)

    def save(self, provider_id: str, credential: Credential) -> None:
        """Blocking write. For CLI paths that have no event loop."""
        data = self._load()
        data[provider_id] = credential
        self._write(data)

    def delete(self, provider_id: str) -> bool:
        """Returns whether anything was removed."""
        data = self._load()
        if data.pop(provider_id, None) is None:
            return False
        self._write(data)
        return True

    async def modify(
        self,
        provider_id: str,
        fn: Callable[[Credential | None], Awaitable[Credential | None]],
    ) -> Credential | None:
        """Serialised read-modify-write.

        ``fn`` receives the current credential and returns the replacement, or
        ``None`` to leave it alone. It sees the current value because the
        correct new value depends on it - a refresh needs the refresh token
        that is on disk now, not the one this process read a minute ago.
        """
        lock = self._locks.setdefault(provider_id, asyncio.Lock())
        async with lock:
            current = await asyncio.to_thread(self.read, provider_id)
            updated = await fn(current)
            if updated is None:
                return current
            await asyncio.to_thread(self.save, provider_id, updated)
            return updated

    def _load(self) -> dict[str, Credential]:
        path = self.path
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return _migrate(raw)

    def _write(self, data: dict[str, Credential]) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {pid: cred.to_json() for pid, cred in sorted(data.items())}

        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        # Restrict before the rename so the token is never briefly world-readable.
        tmp.chmod(0o600)
        tmp.replace(path)


def _migrate(raw: dict[str, Any]) -> dict[str, Credential]:
    """Parse the file, folding the legacy flat key into the tagged form.

    Unparseable entries are dropped rather than raising: one corrupt provider
    entry must not lock the user out of every other provider.
    """
    creds: dict[str, Credential] = {}

    legacy = raw.get(LEGACY_OPENROUTER_KEY)
    if isinstance(legacy, str) and legacy:
        creds[OPENROUTER] = ApiKeyCredential(key=legacy)

    for provider_id, entry in raw.items():
        if provider_id in AuthStore._RESERVED or not isinstance(entry, dict):
            continue
        parsed = _parse(entry)
        if parsed is not None:
            # A tagged entry wins over the legacy key it supersedes.
            creds[provider_id] = parsed
    return creds


def _parse(entry: dict[str, Any]) -> Credential | None:
    kind = entry.get("type")
    if kind == "api_key":
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            return None
        env = entry.get("env")
        return ApiKeyCredential(key=key, env=dict(env) if isinstance(env, dict) else {})

    if kind == "oauth":
        access, refresh = entry.get("access"), entry.get("refresh")
        if not isinstance(access, str) or not isinstance(refresh, str):
            return None
        try:
            expires = float(entry.get("expires", 0.0))
        except (TypeError, ValueError):
            expires = 0.0
        extra = {
            k: v for k, v in entry.items() if k not in {"type", "access", "refresh", "expires"}
        }
        return OAuthCredential(access=access, refresh=refresh, expires=expires, extra=extra)

    return None


def with_tokens(
    credential: OAuthCredential,
    *,
    access: str,
    refresh: str,
    expires: float,
) -> OAuthCredential:
    """Rotate the tokens, keeping provider-specific ``extra`` fields."""
    return replace(credential, access=access, refresh=refresh, expires=expires)


def mask(secret: str) -> str:
    """A fragment safe to display. Never the whole thing."""
    if len(secret) <= 12:
        return "…"
    return f"{secret[:6]}…{secret[-4:]}"
