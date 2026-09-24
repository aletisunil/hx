"""OAuth for remote MCP servers - the client half of the MCP authorization spec.

A server that wants a signed-in user answers an unauthenticated request with
``401`` and a ``WWW-Authenticate: Bearer resource_metadata="..."`` header. From
there the steps are all standard, and none of them is specific to one server:

1. Protected Resource Metadata (RFC 9728) names the authorization server.
2. Authorization Server Metadata (RFC 8414, or OpenID discovery) names its
   endpoints. Issuers with a path are looked up path-aware - Atlassian's is.
3. Dynamic Client Registration (RFC 7591) gets HX a client id, since no id can
   be shipped for servers HX has never heard of. ``oauth.clientId`` in
   ``mcp.json`` skips this for a server that does not offer registration.
4. Authorization code with PKCE (S256), through the same loopback callback and
   paste fallback the model routes use, with ``resource`` (RFC 8707) so the
   token is minted for this server and no other.

Tokens live in ``auth.json`` under ``mcp:<resource url>``, keyed by the server
rather than by its name in ``mcp.json``: renaming an entry, or declaring the
same server in two projects, must not cost a sign-in.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import socket
import time
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from hx import __version__
from hx.auth.oauth.browser import LoginInteraction, OAuthError, authorize_in_browser
from hx.auth.oauth.callback import callback_host
from hx.auth.oauth.pkce import generate_pkce, random_state
from hx.auth.store import AuthStore, OAuthCredential
from hx.net import async_client

log = logging.getLogger(__name__)

CREDENTIAL_PREFIX = "mcp:"
CALLBACK_PATH = "/callback"
LOOPBACK = "127.0.0.1"
HTTP_TIMEOUT = 30.0
CLIENT_NAME = "HX"
CLIENT_URI = "https://hx.sunilaleti.dev"

DEFAULT_LIFETIME = 3600.0
"""For a token response with no ``expires_in``. Short on purpose: guessing low
costs one early refresh, guessing high costs a request that fails first."""

REFRESH_MARGIN = 300.0
"""How long before expiry a token is refreshed, for a token that lives long
enough to spare it. One that does not is refreshed at half its life instead:
a margin wider than the lifetime would call every token stale on arrival."""

PROBE_MESSAGE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "hx", "version": __version__},
    },
}
"""What an unauthenticated probe sends. Any request would draw the 401, but
this is the one a server is guaranteed to route rather than 404."""


@dataclass(frozen=True, slots=True)
class OAuthOptions:
    """The ``oauth`` block of an ``mcp.json`` entry. Everything is optional."""

    client_id: str | None = None
    """A pre-registered client, for a server without dynamic registration."""
    client_secret: str | None = None
    scope: str | None = None
    """Overrides the scopes the server advertises, space-separated."""
    callback_port: int | None = None
    """A fixed loopback port, for a client registered with one redirect URI."""

    @classmethod
    def from_json(cls, raw: Any) -> OAuthOptions:
        if not isinstance(raw, dict):
            return cls()
        port = raw.get("callbackPort")
        return cls(
            client_id=_str_or_none(raw.get("clientId")),
            client_secret=_str_or_none(raw.get("clientSecret")),
            scope=_str_or_none(raw.get("scope")),
            callback_port=int(port)
            if isinstance(port, int | str) and str(port).isdigit()
            else None,
        )

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.client_id:
            data["clientId"] = self.client_id
        if self.client_secret:
            data["clientSecret"] = self.client_secret
        if self.scope:
            data["scope"] = self.scope
        if self.callback_port:
            data["callbackPort"] = self.callback_port
        return data


@dataclass(frozen=True, slots=True)
class Challenge:
    """The parameters of a ``WWW-Authenticate: Bearer`` challenge."""

    resource_metadata: str | None = None
    scope: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AuthServer:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    code_challenge_methods: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Discovery:
    resource: str
    """What the token is minted for, sent as RFC 8707 ``resource``."""
    server: AuthServer
    scope: str | None


@dataclass(frozen=True, slots=True)
class ClientInfo:
    client_id: str
    client_secret: str | None = None
    auth_method: str = "none"
    """``none``, ``client_secret_post`` or ``client_secret_basic``."""
    redirect_uri: str = ""


class SignInNotRequired(OAuthError):
    """The server answered without asking for a token."""


class TokenRejected(OAuthError):
    """The token endpoint refused a grant - a dead refresh token, usually.

    Distinct from a network failure: this one means signing in again, the other
    means trying again.
    """


# -- identifiers ---------------------------------------------------------------


def canonical_resource(url: str) -> str:
    """The server's identity: scheme, host and path, lowercased where case is
    not significant, with no query or fragment.

    ``?tools=all`` and friends change what a server shows, not which server it
    is, so they must not change which token it gets.
    """
    parts = urlsplit(url)
    path = parts.path.rstrip("/") if parts.path not in ("", "/") else ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def credential_id(url: str) -> str:
    return CREDENTIAL_PREFIX + canonical_resource(url)


# -- WWW-Authenticate ------------------------------------------------------------

_PARAM = re.compile(r'([A-Za-z0-9_-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s,]+)')


def parse_www_authenticate(value: str | None) -> Challenge | None:
    """The Bearer challenge in a ``WWW-Authenticate`` header, or ``None``.

    A header can carry several challenges (``Basic realm=..., Bearer ...``);
    only the Bearer one's parameters are read.
    """
    if not value:
        return None
    match = re.search(r"(?:^|[\s,])Bearer(?:\s+|$)", value, flags=re.IGNORECASE)
    if match is None:
        return None
    rest = value[match.end() :]
    # Stop at the next challenge: a scheme name followed by a space, not an `=`.
    next_scheme = re.search(r",\s*[A-Za-z][A-Za-z0-9_-]*\s+[A-Za-z0-9_-]+\s*=", rest)
    if next_scheme is not None:
        rest = rest[: next_scheme.start()]
    params: dict[str, str] = {}
    for key, raw in _PARAM.findall(rest):
        if raw.startswith('"'):
            raw = re.sub(r"\\(.)", r"\1", raw[1:-1])
        params[key.lower()] = raw
    return Challenge(
        resource_metadata=params.get("resource_metadata"),
        scope=params.get("scope"),
        error=params.get("error"),
    )


# -- discovery -------------------------------------------------------------------


async def probe(client: httpx.AsyncClient, url: str) -> Challenge | None:
    """Ask the server unauthenticated. ``None`` when it did not refuse."""
    response = await client.post(
        url,
        json=PROBE_MESSAGE,
        headers={"Accept": "application/json, text/event-stream"},
    )
    if response.status_code != 401:
        return None
    return parse_www_authenticate(response.headers.get("www-authenticate")) or Challenge()


async def discover(
    client: httpx.AsyncClient,
    url: str,
    challenge: Challenge,
    options: OAuthOptions | None = None,
) -> Discovery:
    """Find the authorization server for ``url`` and what to ask it for."""
    options = options or OAuthOptions()
    resource = canonical_resource(url)
    metadata = await _protected_resource_metadata(client, url, challenge)

    scope = options.scope or challenge.scope
    if metadata is not None:
        advertised = metadata.get("resource")
        if isinstance(advertised, str) and advertised:
            if not _covers(advertised, resource):
                # A server naming some other resource as its own would have the
                # token minted for that one - and sent there next.
                raise OAuthError(
                    f"{url} claims to be {advertised}; refusing to sign in to a "
                    "server that names a different resource."
                )
            resource = advertised
        servers = metadata.get("authorization_servers") or []
        issuer = next((s for s in servers if isinstance(s, str) and s), None)
        if issuer is None:
            raise OAuthError(f"{url} names no authorization server.")
        if scope is None:
            scopes = metadata.get("scopes_supported")
            if isinstance(scopes, list) and scopes:
                scope = " ".join(str(s) for s in scopes)
        server = await _authorization_server(client, issuer)
        if server is None:
            raise OAuthError(f"Could not read the metadata of {issuer}.")
    else:
        # A server from before RFC 9728 was adopted (MCP 2025-03-26) is its own
        # authorization server, at fixed paths when it publishes no metadata.
        origin = _origin(url)
        server = await _authorization_server(client, origin) or AuthServer(
            issuer=origin,
            authorization_endpoint=f"{origin}/authorize",
            token_endpoint=f"{origin}/token",
            registration_endpoint=f"{origin}/register",
        )

    if server.code_challenge_methods and "S256" not in server.code_challenge_methods:
        raise OAuthError(f"{server.issuer} does not support PKCE with S256; refusing to sign in.")
    return Discovery(resource=resource, server=server, scope=scope)


async def _protected_resource_metadata(
    client: httpx.AsyncClient, url: str, challenge: Challenge
) -> dict[str, Any] | None:
    if challenge.resource_metadata:
        candidates = [challenge.resource_metadata]
    else:
        origin, path = _origin(url), urlsplit(url).path.rstrip("/")
        candidates = [f"{origin}/.well-known/oauth-protected-resource{path}"] if path else []
        candidates.append(f"{origin}/.well-known/oauth-protected-resource")
    for candidate in candidates:
        document = await _get_json(client, candidate)
        if document is not None:
            return document
    return None


async def _authorization_server(client: httpx.AsyncClient, issuer: str) -> AuthServer | None:
    origin, path = _origin(issuer), urlsplit(issuer).path.rstrip("/")
    if path:
        candidates = [
            f"{origin}/.well-known/oauth-authorization-server{path}",
            f"{origin}/.well-known/openid-configuration{path}",
            f"{origin}{path}/.well-known/openid-configuration",
            f"{origin}{path}/.well-known/oauth-authorization-server",
        ]
    else:
        candidates = [
            f"{origin}/.well-known/oauth-authorization-server",
            f"{origin}/.well-known/openid-configuration",
        ]
    impostor: str | None = None
    for candidate in candidates:
        document = await _get_json(client, candidate)
        if document is None:
            continue
        authorize = document.get("authorization_endpoint")
        token = document.get("token_endpoint")
        if not isinstance(authorize, str) or not isinstance(token, str):
            continue
        claimed = _str_or_none(document.get("issuer")) or issuer
        if claimed.rstrip("/") != issuer.rstrip("/"):
            # RFC 8414 section 3.3: metadata naming another issuer must not be
            # used. Followed, it sends the code and the token to a server other
            # than the one the resource named - the mix-up attack. Another of
            # the well-known paths may still hold the right document.
            impostor = impostor or (
                f"The authorization server metadata at {candidate} names {claimed} as "
                f"its issuer, not {issuer}; refusing to sign in."
            )
            continue
        methods = document.get("code_challenge_methods_supported")
        return AuthServer(
            issuer=claimed,
            authorization_endpoint=authorize,
            token_endpoint=token,
            registration_endpoint=_str_or_none(document.get("registration_endpoint")),
            code_challenge_methods=tuple(str(m) for m in methods)
            if isinstance(methods, list)
            else (),
        )
    if impostor is not None:
        raise OAuthError(impostor)
    return None


async def _get_json(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    try:
        response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        log.debug("oauth metadata %s unreachable: %s", url, exc)
        return None
    if response.status_code != 200:
        return None
    try:
        document = response.json()
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


# -- registration and the grant ---------------------------------------------------


async def register(
    client: httpx.AsyncClient,
    server: AuthServer,
    redirect_uri: str,
    scope: str | None,
) -> ClientInfo:
    if not server.registration_endpoint:
        raise OAuthError(
            f"{server.issuer} does not offer dynamic client registration. Register a "
            'client with it and set "oauth": {"clientId": "..."} on this server in mcp.json.'
        )
    body: dict[str, Any] = {
        "client_name": CLIENT_NAME,
        "client_uri": CLIENT_URI,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scope:
        body["scope"] = scope
    response = await client.post(server.registration_endpoint, json=body)
    if response.status_code >= 400:
        raise OAuthError(
            f"Client registration at {server.registration_endpoint} failed "
            f"({response.status_code}): {response.text[:300]}"
        )
    try:
        data = response.json()
    except ValueError:
        raise OAuthError(
            f"Client registration at {server.registration_endpoint} answered "
            f"{response.status_code} with a body that is not JSON: "
            f"{' '.join(response.text.split())[:200]}"
        ) from None
    client_id = data.get("client_id") if isinstance(data, dict) else None
    if not isinstance(client_id, str) or not client_id:
        raise OAuthError(f"{server.registration_endpoint} returned no client_id.")
    secret = _str_or_none(data.get("client_secret"))
    method = _str_or_none(data.get("token_endpoint_auth_method")) or (
        "client_secret_basic" if secret else "none"
    )
    return ClientInfo(client_id, secret, method, redirect_uri)


def authorize_url(discovery: Discovery, client: ClientInfo, challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": discovery.resource,
    }
    if discovery.scope:
        params["scope"] = discovery.scope
    endpoint = discovery.server.authorization_endpoint
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(params)}"


async def _token_request(
    http: httpx.AsyncClient, endpoint: str, form: dict[str, str], client: ClientInfo
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    form = dict(form)
    if client.auth_method == "client_secret_basic" and client.client_secret:
        pair = f"{client.client_id}:{client.client_secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
    else:
        form["client_id"] = client.client_id
        if client.client_secret:
            form["client_secret"] = client.client_secret
    response = await http.post(endpoint, data=form, headers=headers)
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        error = data.get("error") if isinstance(data, dict) else None
        detail = (data.get("error_description") if isinstance(data, dict) else None) or ""
        message = f"{endpoint} refused the grant ({response.status_code}"
        message += f", {error}): {detail}" if error else f"): {response.text[:300]}"
        raise TokenRejected(message.rstrip(": "))
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
        raise OAuthError(f"{endpoint} returned no access_token.")
    return data


def _credential(
    token: dict[str, Any],
    *,
    server_url: str,
    discovery: Discovery,
    client: ClientInfo,
    previous_refresh: str = "",
) -> OAuthCredential:
    try:
        lifetime = float(token.get("expires_in") or DEFAULT_LIFETIME)
    except (TypeError, ValueError):
        lifetime = DEFAULT_LIFETIME
    extra: dict[str, Any] = {
        "lifetime": lifetime,
        "server_url": server_url,
        "resource": discovery.resource,
        "issuer": discovery.server.issuer,
        "token_endpoint": discovery.server.token_endpoint,
        "client_id": client.client_id,
        "auth_method": client.auth_method,
        "redirect_uri": client.redirect_uri,
    }
    if client.client_secret:
        extra["client_secret"] = client.client_secret
    if discovery.scope:
        extra["scope"] = discovery.scope
    return OAuthCredential(
        access=str(token["access_token"]),
        # A server that does not rotate refresh tokens leaves the field out of
        # the refresh response; the one it issued first is still the one to use.
        refresh=str(token.get("refresh_token") or previous_refresh),
        expires=time.time() + lifetime,
        extra=extra,
    )


# -- the two entry points ----------------------------------------------------------


async def login(
    url: str,
    interaction: LoginInteraction,
    options: OAuthOptions | None = None,
    *,
    label: str = "",
    store: AuthStore | None = None,
) -> OAuthCredential:
    """Run the browser sign-in for the server at ``url``. Does not save.

    Raises:
        SignInNotRequired: when the server answers without a token.
        OAuthError: when discovery, registration or the grant fails.
        CallbackError: when the browser step fails or is abandoned.
    """
    options = options or OAuthOptions()
    store = store or AuthStore()
    interaction.progress(f"Looking up how {label or url} signs in…")
    async with async_client(timeout=HTTP_TIMEOUT) as http:
        try:
            challenge = await probe(http, url)
            if challenge is None:
                raise SignInNotRequired(f"{label or url} does not ask for sign-in.")
            discovery = await discover(http, url, challenge, options)
            client = await _client_for(http, url, discovery, options, store)
        except httpx.HTTPError as exc:
            raise OAuthError(f"Could not reach {url}: {exc}") from exc

        pkce = generate_pkce()
        state = random_state()
        result = await authorize_in_browser(
            authorize_url(discovery, client, pkce.challenge, state),
            port=_port_of(client.redirect_uri),
            path=CALLBACK_PATH,
            state=state,
            interaction=interaction,
        )
        interaction.progress("Exchanging the authorization code…")
        try:
            token = await _token_request(
                http,
                discovery.server.token_endpoint,
                {
                    "grant_type": "authorization_code",
                    "code": result.code,
                    "redirect_uri": client.redirect_uri,
                    "code_verifier": pkce.verifier,
                    "resource": discovery.resource,
                },
                client,
            )
        except httpx.HTTPError as exc:
            raise OAuthError(f"Could not reach {discovery.server.token_endpoint}: {exc}") from exc
    return _credential(token, server_url=url, discovery=discovery, client=client)


async def refresh(credential: OAuthCredential) -> OAuthCredential:
    """Trade the refresh token for a new pair, from what the credential recorded.

    Raises:
        TokenRejected: when the refresh token is no longer accepted.
        httpx.HTTPError: when the token endpoint cannot be reached.
    """
    extra = credential.extra
    endpoint = str(extra.get("token_endpoint") or "")
    if not credential.refresh or not endpoint:
        raise TokenRejected("There is no refresh token to use.")
    client = ClientInfo(
        client_id=str(extra.get("client_id") or ""),
        client_secret=_str_or_none(extra.get("client_secret")),
        auth_method=str(extra.get("auth_method") or "none"),
        redirect_uri=str(extra.get("redirect_uri") or ""),
    )
    form = {"grant_type": "refresh_token", "refresh_token": credential.refresh}
    if resource := extra.get("resource"):
        form["resource"] = str(resource)
    async with async_client(timeout=HTTP_TIMEOUT) as http:
        token = await _token_request(http, endpoint, form, client)
    refreshed = _credential(
        token,
        server_url=str(extra.get("server_url") or ""),
        discovery=Discovery(
            resource=str(extra.get("resource") or ""),
            server=AuthServer(
                issuer=str(extra.get("issuer") or ""),
                authorization_endpoint="",
                token_endpoint=endpoint,
            ),
            scope=_str_or_none(extra.get("scope")),
        ),
        client=client,
        previous_refresh=credential.refresh,
    )
    # Fields this module did not write are carried over rather than dropped.
    return replace(refreshed, extra={**extra, **refreshed.extra})


async def _client_for(
    http: httpx.AsyncClient,
    url: str,
    discovery: Discovery,
    options: OAuthOptions,
    store: AuthStore,
) -> ClientInfo:
    """The client to sign in as: configured, reused from last time, or new.

    A registration is reused while its loopback port is free, so signing in
    again does not leave a trail of dead clients on the server. A port that is
    now taken means a fresh registration on a free one, since the redirect URI
    is matched exactly.
    """
    if options.client_id:
        port = options.callback_port or _free_port()
        return ClientInfo(
            client_id=options.client_id,
            client_secret=options.client_secret,
            auth_method="client_secret_post" if options.client_secret else "none",
            redirect_uri=_redirect_uri(port),
        )

    previous = await asyncio.to_thread(store.read, credential_id(url))
    if isinstance(previous, OAuthCredential):
        extra = previous.extra
        redirect = str(extra.get("redirect_uri") or "")
        if (
            extra.get("issuer") == discovery.server.issuer
            and extra.get("client_id")
            and redirect
            and (options.callback_port in (None, _port_of(redirect)))
            and _port_is_free(_port_of(redirect))
        ):
            return ClientInfo(
                client_id=str(extra["client_id"]),
                client_secret=_str_or_none(extra.get("client_secret")),
                auth_method=str(extra.get("auth_method") or "none"),
                redirect_uri=redirect,
            )

    port = options.callback_port or _free_port()
    return await register(http, discovery.server, _redirect_uri(port), discovery.scope)


# -- the token a transport sends -----------------------------------------------------


@dataclass(slots=True)
class OAuthSession:
    """Supplies one server's bearer token, refreshing it when it runs out.

    Reads the store rather than holding the token for the session: a sign-in
    in another terminal, or a refresh by another HX, has to be picked up here
    without a restart.
    """

    url: str
    store: AuthStore = field(default_factory=AuthStore)
    _cached: OAuthCredential | None = None

    @property
    def key(self) -> str:
        return credential_id(self.url)

    async def token(self) -> str | None:
        """The access token to send, or ``None`` when never signed in."""
        credential = self._cached
        if credential is None or _stale(credential):
            credential = await self._load()
        if credential is None:
            return None
        if _stale(credential) and credential.refresh:
            credential = await self._refresh(credential.access) or credential
        return credential.access

    async def recover(self, rejected: str | None) -> str | None:
        """After a 401: a token other than ``rejected``, or ``None``.

        Another process may already hold a fresher token, which is checked
        before spending the refresh token.
        """
        credential = await self._load()
        if credential is None:
            return None
        if credential.access != rejected:
            return credential.access
        refreshed = await self._refresh(rejected)
        return refreshed.access if refreshed is not None else None

    async def _load(self) -> OAuthCredential | None:
        credential = await asyncio.to_thread(self.store.read, self.key)
        self._cached = credential if isinstance(credential, OAuthCredential) else None
        return self._cached

    async def _refresh(self, stale: str | None) -> OAuthCredential | None:
        """Refresh under the store's lock. ``None`` when the refresh token is dead.

        The check against ``stale`` is repeated inside the lock: a second
        request that queued behind the first refresh must use its result, not
        burn the refresh token that result just replaced.
        """

        async def swap(current: Any) -> OAuthCredential | None:
            if not isinstance(current, OAuthCredential) or current.access != stale:
                return None
            try:
                return await refresh(current)
            except TokenRejected as exc:
                log.info("mcp %s: refresh refused: %s", self.url, exc)
                return None

        result = await self.store.modify(self.key, swap)
        if not isinstance(result, OAuthCredential) or result.access == stale:
            return None
        self._cached = result
        return result


# -- helpers --------------------------------------------------------------------


def _stale(credential: OAuthCredential) -> bool:
    """Whether to refresh before sending: inside :data:`REFRESH_MARGIN` of
    expiry, or past half its life if it was issued for less than twice that."""
    try:
        lifetime = float(credential.extra.get("lifetime") or 0.0)
    except (TypeError, ValueError):
        lifetime = 0.0
    margin = min(REFRESH_MARGIN, lifetime / 2) if lifetime > 0 else REFRESH_MARGIN
    return credential.expired(leeway=margin)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _covers(advertised: str, resource: str) -> bool:
    """Whether a token for ``advertised`` is a token for ``resource``."""
    a, r = urlsplit(canonical_resource(advertised)), urlsplit(resource)
    if (a.scheme, a.netloc) != (r.scheme, r.netloc):
        return False
    return r.path == a.path or r.path.startswith(a.path.rstrip("/") + "/") or not a.path


def _redirect_uri(port: int) -> str:
    # The loopback literal, not "localhost" (RFC 8252 section 7.3): it is what
    # the callback binds, and "localhost" can resolve to ::1 first. Not the
    # callback host either - a container binding 0.0.0.0 is still reached by a
    # browser on 127.0.0.1.
    return f"http://{LOOPBACK}:{port}{CALLBACK_PATH}"


def _port_of(redirect_uri: str) -> int:
    return urlsplit(redirect_uri).port or 80


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((callback_host(), 0))
        return int(sock.getsockname()[1])


def _port_is_free(port: int) -> bool:
    """Whether the callback could bind ``port`` - asked the way it binds.

    ``SO_REUSEADDR`` as ``HTTPServer`` sets it: the last sign-in's callback
    leaves its port in TIME_WAIT, which blocks a plain bind but not the server.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((callback_host(), port))
        except OSError:
            return False
    return True


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
