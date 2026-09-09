"""Credential storage and OAuth flows.

One credential per provider id, stored in ``~/.hx/auth.json``. Providers ask
:func:`hx.auth.resolve.resolve` for request auth; they never read the file or
the environment themselves.
"""

from hx.auth.store import (
    ApiKeyCredential,
    AuthStore,
    Credential,
    CredentialInfo,
    OAuthCredential,
)

__all__ = [
    "ApiKeyCredential",
    "AuthStore",
    "Credential",
    "CredentialInfo",
    "OAuthCredential",
]
