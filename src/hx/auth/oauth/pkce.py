"""PKCE (RFC 7636) code verifier and S256 challenge.

Stdlib only - this is thirty lines of hashing and there is no reason to take a
dependency for it.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PKCE:
    verifier: str
    challenge: str
    method: str = "S256"


def b64url(raw: bytes) -> str:
    """base64url without padding, as every OAuth spec wants it."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def challenge_for(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def generate_pkce(*, entropy_bytes: int = 32) -> PKCE:
    """A fresh verifier/challenge pair.

    ``entropy_bytes`` of 32 yields a 43-character verifier, the shortest length
    the spec allows and the one every reference client uses.
    """
    verifier = b64url(secrets.token_bytes(entropy_bytes))
    return PKCE(verifier=verifier, challenge=challenge_for(verifier))


def random_state() -> str:
    return secrets.token_hex(16)
