"""One TLS policy for every outbound request.

httpx verifies against certifi's bundle, which is not the machine's trust
store. On a network that terminates TLS - a corporate proxy, a VPN client, a
security agent - the interception CA lives in the OS store, so every browser on
the box works and hx alone fails with "certificate verify failed". So the trust
store is decided here, once, and every client is built through
:func:`async_client`:

* ``HX_CA_BUNDLE`` (or ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE``) - an explicit
  PEM file wins, because a user who names a bundle means it.
* the OS trust store via ``truststore``, so a CA the machine already trusts is
  enough and nothing has to be exported by hand.
* certifi, as httpx would have done anyway.

``HX_SSL_NO_VERIFY=1`` turns verification off. It exists because a blocked
user will otherwise reach for a worse workaround; it says so out loud in
:func:`tls_notice` rather than failing quietly open.
"""

from __future__ import annotations

import os
import re
import ssl
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

CA_BUNDLE_VARS: tuple[str, ...] = ("HX_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
NO_VERIFY_VAR = "HX_SSL_NO_VERIFY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def ca_bundle() -> Path | None:
    """The CA bundle the environment names, if it points at a real file."""
    for name in CA_BUNDLE_VARS:
        raw = (os.environ.get(name) or "").strip()
        if raw and Path(raw).expanduser().is_file():
            return Path(raw).expanduser()
    return None


def verification_disabled() -> bool:
    return (os.environ.get(NO_VERIFY_VAR) or "").strip().lower() in _TRUTHY


@lru_cache(maxsize=1)
def ssl_verify() -> ssl.SSLContext | bool:
    """What to pass as httpx's ``verify``. Cached: building a context reads
    every certificate in the store."""
    if verification_disabled():
        return False
    bundle = ca_bundle()
    if bundle is not None:
        return _finish(ssl.create_default_context(cafile=str(bundle)))
    try:
        import truststore
    except ImportError:  # pragma: no cover - truststore is a hard dependency
        return True
    return _finish(truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))


def _finish(context: ssl.SSLContext) -> ssl.SSLContext:
    # httpx negotiates this on its own default context; a supplied one keeps
    # whatever we set, and an empty ALPN list breaks HTTP/1.1 on some proxies.
    context.set_alpn_protocols(["http/1.1"])
    return context


def tls_notice() -> str | None:
    """One line for the startup transcript when TLS is not stock."""
    if verification_disabled():
        return f"{NO_VERIFY_VAR} is set: TLS certificates are not being verified."
    bundle = ca_bundle()
    return f"Verifying TLS against {bundle}." if bundle is not None else None


def async_client(**kwargs: Any) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` with hx's trust store already applied."""
    kwargs.setdefault("verify", ssl_verify())
    return httpx.AsyncClient(**kwargs)


def describe(exc: BaseException) -> str:
    """A network failure in one line, naming the fix when there is one.

    Callers put this in a notice, so the raw exception - which for a TLS
    failure is three nested frames deep and 200 characters wide - is no use.
    """
    for err in _chain(exc):
        if isinstance(err, ssl.SSLCertVerificationError):
            # A constructed error carries neither field; only OpenSSL fills them in.
            reason = (
                getattr(err, "verify_message", None)
                or getattr(err, "reason", None)
                or "certificate verify failed"
            )
            return (
                f"TLS certificate verification failed ({_flatten(reason)}). "
                f"If your network intercepts TLS, add its CA to the system trust store "
                f"or set {CA_BUNDLE_VARS[0]} to its PEM file."
            )
        if isinstance(err, ssl.SSLError):
            return f"TLS handshake failed: {_flatten(err.reason or str(err))}."
    if isinstance(exc, httpx.TimeoutException):
        return f"Timed out talking to {_host(exc)}."
    if isinstance(exc, httpx.ConnectError):
        return f"Cannot reach {_host(exc)}: {_flatten(str(exc))}."
    return f"{type(exc).__name__}: {_flatten(str(exc))}"


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and not any(current is s for s in seen):
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def _host(exc: BaseException) -> str:
    request = getattr(exc, "request", None)
    return str(request.url.host) if request is not None else "the server"


def _flatten(text: str, limit: int = 160) -> str:
    collapsed = re.sub(r"\s+", " ", str(text)).strip().rstrip(".")
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"
