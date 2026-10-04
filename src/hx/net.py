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

Not every request is HX's own. The ``claude-code`` route runs the ``claude``
CLI, a Node program with its own TLS stack, which never sees an
``SSLContext`` built here. :func:`node_tls_env` restates the same policy in the
variables Node reads, so a setting that fixes one route fixes them all.
"""

from __future__ import annotations

import os
import re
import ssl
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

CA_BUNDLE_VARS: tuple[str, ...] = ("HX_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
NO_VERIFY_VAR = "HX_SSL_NO_VERIFY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def ca_bundle() -> Path | None:
    """The CA bundle the environment names, if it points at a file of certificates."""
    named = _named_bundle()
    if named is None or _load_error(named[1]) is not None:
        return None
    return named[1]


def _named_bundle() -> tuple[str, Path] | None:
    """The first of :data:`CA_BUNDLE_VARS` that names a real file, and that file."""
    for name in CA_BUNDLE_VARS:
        raw = (os.environ.get(name) or "").strip()
        if raw and Path(raw).expanduser().is_file():
            return name, Path(raw).expanduser()
    return None


def _load_error(path: Path) -> str | None:
    """Why ``path`` cannot serve as a CA bundle - a DER file, a key, an empty
    file - or ``None`` when it can. Asked again only when the file changes."""
    try:
        stat = path.stat()
    except OSError as exc:
        return exc.strerror or str(exc)
    return _load_error_of(path, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4)
def _load_error_of(path: Path, mtime_ns: int, size: int) -> str | None:
    try:
        ssl.create_default_context(cafile=str(path))
    except (ssl.SSLError, OSError, ValueError) as exc:
        return _flatten(getattr(exc, "reason", None) or str(exc))
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
    named = _named_bundle()
    if named is not None and (error := _load_error(named[1])) is not None:
        # Ignored, as OpenSSL and Node ignore a CA file they cannot read: failing
        # to start would cost every route, the ones this file is not for too.
        name, path = named
        return (
            f"{name} names {path}, which holds no usable CA certificates ({error}). "
            f"Verifying TLS against the system trust store instead."
        )
    bundle = ca_bundle()
    return f"Verifying TLS against {bundle}." if bundle is not None else None


def node_tls_env(environ: Mapping[str, str]) -> dict[str, str]:
    """What to add to a Node child's ``environ`` so it trusts what HX trusts.

    * A bundle HX verifies against becomes ``NODE_EXTRA_CA_CERTS``, which Node
      adds to its own roots and the OS store. One the user already gave Node
      is theirs and stays: they set it for Node, and it already worked there.
    * ``HX_SSL_NO_VERIFY`` becomes ``NODE_TLS_REJECT_UNAUTHORIZED=0``.

    A machine whose OS store already holds the CA needs neither: Claude Code
    reads the OS store itself.
    """
    added: dict[str, str] = {}
    bundle = ca_bundle()
    if bundle is not None and not (environ.get("NODE_EXTRA_CA_CERTS") or "").strip():
        added["NODE_EXTRA_CA_CERTS"] = str(bundle)
    if verification_disabled():
        added["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    return added


_CERT_FAILURE = re.compile(
    r"certificate verif"
    r"|self[- ]signed certificate"
    r"|unable to (?:get local issuer|verify the first) certificate"
    r"|UNABLE_TO_VERIFY_LEAF_SIGNATURE|UNABLE_TO_GET_ISSUER_CERT|SELF_SIGNED_CERT_IN_CHAIN"
    r"|certificate purpose|INVALID_PURPOSE",
    re.IGNORECASE,
)

_WRONG_PURPOSE = re.compile(r"(?:unsupported|unsuitable) certificate purpose|INVALID_PURPOSE")
"""A chain the client does trust, but with a certificate in it - typically the
issuing CA of a network that intercepts TLS - that may not serve as a TLS
server's: OpenSSL says "unsuitable", the BoringSSL in Claude Code "unsupported"."""


def tls_hint(reason: str = "") -> str:
    """What to do about a certificate the client would not accept.

    ``reason`` is the client's own words, when there are any: a certificate of
    the wrong purpose is already trusted, so adding its CA again changes nothing.
    """
    if _WRONG_PURPOSE.search(reason):
        return (
            "Your network intercepts TLS with a certificate that is not valid for servers. "
            "Ask its administrator to fix that certificate or to exclude this service from "
            f"inspection; until then, {NO_VERIFY_VAR}=1 turns verification off."
        )
    bundle = ca_bundle()
    if bundle is not None:
        return (
            f"HX is verifying TLS against {bundle}; check that it holds the CA "
            f"your network signs with."
        )
    return (
        f"If your network intercepts TLS, add its CA to the system trust store "
        f"or set {CA_BUNDLE_VARS[0]} to its PEM file."
    )


def with_tls_hint(message: str) -> str:
    """``message``, followed by :func:`tls_hint` when it reports a certificate
    failure - in any client's words: OpenSSL's, Node's, Claude Code's."""
    hint = tls_hint(message)
    if not _CERT_FAILURE.search(message) or hint in message:
        return message
    return f"{message.rstrip().rstrip('.')}. {hint}"


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
            reason = _flatten(reason)
            return f"TLS certificate verification failed ({reason}). {tls_hint(reason)}"
        if isinstance(err, ssl.SSLError):
            return f"TLS handshake failed: {_flatten(err.reason or str(err))}."
    if isinstance(exc, httpx.TimeoutException):
        return f"Timed out talking to {_host(exc)}."
    if isinstance(exc, httpx.ConnectError):
        return f"Cannot reach {_host(exc)}: {_flatten(str(exc))}."
    # A child process's failure arrives as text, already flattened by whoever
    # raised it: the hint is all there is left to add.
    return with_tls_hint(f"{type(exc).__name__}: {_flatten(str(exc))}")


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
