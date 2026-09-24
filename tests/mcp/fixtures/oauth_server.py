"""A remote MCP server behind OAuth, for tests.

Real HTTP on a real port, serving both halves the way a hosted server does:
the MCP endpoint (a protected resource) and a separate authorization server
under a path-bearing issuer - the shape Atlassian's has. Every step a client
can get wrong is checked here rather than trusted: the registered redirect URI,
PKCE, the ``resource`` parameter, the refresh token.

Knobs are attributes, flipped by the test mid-run: revoke tokens, forget
sessions, open the GET stream, announce a new tool.

Sessions are kept the way the SDK servers keep them: the GET stream belongs to
one session and carries only that session's messages, a stale session id is
answered 404 there as on POST, and an ``initialize`` that already carries a
session id is refused.
"""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

ECHO = {
    "name": "echo",
    "description": "Echo a message back",
    "inputSchema": {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
}
UNLOCK = {
    "name": "unlock",
    "description": "Reveal another tool, announcing it in the reply",
    "inputSchema": {"type": "object", "properties": {}},
}
SECRET = {
    "name": "secret",
    "description": "Only listed once unlocked",
    "inputSchema": {"type": "object", "properties": {}},
}


class OAuthMCPServer:
    def __init__(self, *, access_ttl: int = 3600, get_stream: bool = False) -> None:
        self.access_ttl = access_ttl
        self.get_stream = get_stream
        self.static_token: str | None = None
        """Accepted as-is, standing in for an API token in a header."""
        self.open_access = False
        """No token needed at all: a public server."""
        self.metadata_issuer: str | None = None
        """What the authorization server metadata claims as its issuer, when it
        should not be the issuer it was looked up under."""
        self.registration_reply: bytes | None = None
        """A raw ``200`` body for client registration, in place of JSON."""

        self.clients: dict[str, list[str]] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.access: dict[str, float] = {}
        self.refresh: set[str] = set()
        self.sessions: set[str] = set()
        self.tools = [ECHO, UNLOCK]

        self.registrations = 0
        self.refreshes = 0
        self.initializes = 0
        self.refused_initializes = 0
        self.stream_refusals = 0
        """GET streams answered 404 for a session this server no longer has."""
        self.authorize_requests: list[dict[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.pinged = threading.Event()

        self._events: dict[str, queue.Queue[dict[str, Any]]] = {}
        """Per live session, what its GET stream has yet to carry."""
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._httpd.daemon_threads = True
        # Polled for shutdown every 20ms rather than the default 500ms: every
        # test stops one of these, and would otherwise wait out the interval.
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    # -- lifecycle -------------------------------------------------------------

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    @property
    def url(self) -> str:
        return f"{self.base}/mcp"

    @property
    def issuer(self) -> str:
        return f"{self.base}/tenant"

    def start(self) -> OAuthMCPServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopping.set()
        self._httpd.shutdown()
        self._httpd.server_close()

    # -- knobs -----------------------------------------------------------------

    def revoke_access(self) -> None:
        self.access.clear()

    def revoke_all(self) -> None:
        self.access.clear()
        self.refresh.clear()

    def forget_sessions(self) -> None:
        """What a restart or an idle timeout does: every session, and every
        stream that belonged to one, is gone."""
        with self._lock:
            self.sessions.clear()
            self._events.clear()

    def announce(self, tool: dict[str, Any]) -> None:
        """Add a tool and say so on each session's GET stream, outside any request."""
        self.tools.append(tool)
        self._broadcast({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    def ping_client(self) -> None:
        self._broadcast({"jsonrpc": "2.0", "id": 99, "method": "ping"})

    def _broadcast(self, event: dict[str, Any]) -> None:
        with self._lock:
            for events in self._events.values():
                events.put(event)

    def _open_session(self) -> str:
        session = secrets.token_hex(8)
        with self._lock:
            self.sessions.add(session)
            self._events[session] = queue.Queue()
        return session

    # -- token issue -------------------------------------------------------------

    def _issue(self) -> dict[str, Any]:
        access, refresh = secrets.token_hex(8), secrets.token_hex(8)
        self.access[access] = time.time() + self.access_ttl
        self.refresh.add(refresh)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": self.access_ttl,
        }

    def authorized(self, header: str | None) -> bool:
        if self.open_access:
            return True
        if not header or not header.startswith("Bearer "):
            return False
        token = header.removeprefix("Bearer ")
        if self.static_token is not None and token == self.static_token:
            return True
        expiry = self.access.get(token)
        return expiry is not None and expiry > time.time()


def _handler(server: OAuthMCPServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        # -- plumbing ----------------------------------------------------------

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def _send(
            self,
            status: int,
            body: Any = None,
            *,
            headers: dict[str, str] | None = None,
            content_type: str = "application/json",
        ) -> None:
            payload = (
                b""
                if body is None
                else (body if isinstance(body, bytes) else json.dumps(body).encode())
            )
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            if payload:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _unauthorized(self) -> None:
            metadata = f"{server.base}/.well-known/oauth-protected-resource/mcp"
            self._send(
                401,
                {"error": "invalid_token"},
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'},
            )

        # -- GET ---------------------------------------------------------------

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/.well-known/oauth-protected-resource/mcp":
                self._send(
                    200,
                    {
                        "resource": server.url,
                        "authorization_servers": [server.issuer],
                        "scopes_supported": ["read", "offline_access"],
                    },
                )
            elif path == "/.well-known/oauth-authorization-server/tenant":
                self._send(
                    200,
                    {
                        "issuer": server.metadata_issuer or server.issuer,
                        "authorization_endpoint": f"{server.base}/authorize",
                        "token_endpoint": f"{server.base}/oauth/token",
                        "registration_endpoint": f"{server.issuer}/register",
                        "code_challenge_methods_supported": ["S256"],
                    },
                )
            elif path == "/authorize":
                self._authorize()
            elif path == "/mcp":
                self._stream()
            else:
                self._send(404, {"error": "not_found"})

        def _authorize(self) -> None:
            query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            server.authorize_requests.append(query)
            redirect = query.get("redirect_uri", "")
            if redirect not in server.clients.get(query.get("client_id", ""), []):
                self._send(400, {"error": "invalid_redirect_uri"})
                return
            if query.get("code_challenge_method") != "S256" or query.get("resource") != server.url:
                self._send(400, {"error": "invalid_request"})
                return
            code = secrets.token_hex(8)
            server.codes[code] = query
            location = f"{redirect}?{urlencode({'code': code, 'state': query['state']})}"
            self._send(302, headers={"Location": location})

        def _stream(self) -> None:
            if not server.get_stream:
                self._send(405, {"error": "no stream"})
                return
            if not server.authorized(self.headers.get("Authorization")):
                self._unauthorized()
                return
            session = self.headers.get("Mcp-Session-Id") or ""
            events = server._events.get(session)
            if events is None:
                server.stream_refusals += 1
                self._send(404, {"error": "unknown session"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            # No length, so the stream ends when the connection does.
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            # Ends with its session, as a restarted server's streams do.
            while not server._stopping.is_set() and session in server.sessions:
                try:
                    event = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    self.wfile.write(f"id: {secrets.token_hex(4)}\n".encode())
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
                except OSError:
                    return

        # -- POST --------------------------------------------------------------

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path == "/tenant/register":
                self._register()
            elif path == "/oauth/token":
                self._token()
            elif path == "/mcp":
                self._rpc()
            else:
                self._send(404, {"error": "not_found"})

        def _register(self) -> None:
            body = json.loads(self._body())
            if server.registration_reply is not None:
                self._send(200, server.registration_reply, content_type="text/html")
                return
            client_id = f"client-{secrets.token_hex(4)}"
            server.clients[client_id] = list(body.get("redirect_uris") or [])
            server.registrations += 1
            self._send(201, {"client_id": client_id, **body})

        def _token(self) -> None:
            form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
            server.token_requests.append(form)
            if form.get("resource") != server.url:
                self._send(400, {"error": "invalid_target"})
                return
            grant = form.get("grant_type")
            if grant == "authorization_code":
                issued = server.codes.pop(form.get("code", ""), None)
                verifier = form.get("code_verifier", "")
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                    .decode()
                    .rstrip("=")
                )
                if (
                    issued is None
                    or issued["code_challenge"] != challenge
                    or issued["redirect_uri"] != form.get("redirect_uri")
                    or issued["client_id"] != form.get("client_id")
                ):
                    self._send(400, {"error": "invalid_grant"})
                    return
                self._send(200, server._issue())
            elif grant == "refresh_token":
                token = form.get("refresh_token", "")
                if token not in server.refresh:
                    self._send(400, {"error": "invalid_grant", "error_description": "revoked"})
                    return
                server.refresh.discard(token)
                server.refreshes += 1
                self._send(200, server._issue())
            else:
                self._send(400, {"error": "unsupported_grant_type"})

        def _rpc(self) -> None:
            body = self._body()
            if not server.authorized(self.headers.get("Authorization")):
                self._unauthorized()
                return
            message = json.loads(body)
            method = message.get("method")
            session = self.headers.get("Mcp-Session-Id")

            if method == "initialize":
                if session is not None:
                    # As the SDK servers answer it: a session is not
                    # initialized twice, and one id is not a second session.
                    server.refused_initializes += 1
                    self._send(400, {"error": "session already initialized"})
                    return
                server.initializes += 1
                session = server._open_session()
                self._send(
                    200,
                    _result(
                        message,
                        {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {"listChanged": True}},
                            "serverInfo": {"name": "oauth-fixture", "version": "0"},
                        },
                    ),
                    headers={"Mcp-Session-Id": session},
                )
                return
            if session not in server.sessions:
                self._send(404, {"error": "unknown session"})
                return
            if "method" not in message:
                # A reply to something the server asked.
                if message.get("id") == 99 and "result" in message:
                    server.pinged.set()
                self._send(202)
                return
            if method.startswith("notifications/"):
                self._send(202)
                return
            if method == "tools/list":
                self._send(200, _result(message, {"tools": server.tools}))
                return
            if method == "tools/call":
                self._call(message)
                return
            self._send(200, {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601}})

        def _call(self, message: dict[str, Any]) -> None:
            name = message["params"]["name"]
            arguments = message["params"].get("arguments") or {}
            if name == "echo":
                text = str(arguments.get("message", ""))
                self._send(200, _result(message, {"content": [{"type": "text", "text": text}]}))
                return
            if name == "unlock":
                if SECRET not in server.tools:
                    server.tools.append(SECRET)
                # The notification rides ahead of the reply on the same stream,
                # split over several data lines as SSE allows.
                notice = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
                reply = _result(message, {"content": [{"type": "text", "text": "unlocked"}]})
                split = "".join(
                    f"data: {line}\n" for line in json.dumps(reply, indent=1).splitlines()
                )
                stream = f"data: {json.dumps(notice)}\n\n{split}\n".encode()
                self._send(200, stream, content_type="text/event-stream")
                return
            self._send(
                200,
                _result(message, {"content": [{"type": "text", "text": name}], "isError": False}),
            )

    return Handler


def _result(message: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}
