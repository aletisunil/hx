"""Loopback listener for the OAuth authorization-code redirect.

The provider redirects the browser to ``http://localhost:<port>/<path>``; this
serves that one request, checks ``state``, and hands the code back.

It binds ``127.0.0.1`` rather than ``0.0.0.0``: the redirect only ever comes
from this machine, and a wider bind would let anything on the network post a
forged code. ``$HX_OAUTH_CALLBACK_HOST`` overrides it for container setups
where the browser reaches the host by another address.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

HOST_ENV = "HX_OAUTH_CALLBACK_HOST"
DEFAULT_HOST = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class CallbackResult:
    code: str
    state: str | None = None


class CallbackError(Exception):
    pass


def callback_host() -> str:
    return os.environ.get(HOST_ENV) or DEFAULT_HOST


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>HX</title><style>
body{{background:#111418;color:#e6e6e6;font:15px/1.6 ui-sans-serif,system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{text-align:center;max-width:28rem;padding:2rem}}
h1{{font-size:1.1rem;margin:0 0 .5rem;color:{colour}}}
p{{margin:0;color:#9aa4b2}}
</style></head><body><div class="card"><h1>{title}</h1><p>{body}</p></div></body></html>
"""


def _page(title: str, body: str, *, ok: bool) -> bytes:
    return _PAGE.format(title=title, body=body, colour="#7ee787" if ok else "#ff7b72").encode()


class _Handler(BaseHTTPRequestHandler):
    server: Any  # set by HTTPServer; carries the fields assigned in serve_once

    def do_GET(self) -> None:  # BaseHTTPRequestHandler dispatches on this name
        parsed = urlparse(self.path)
        if parsed.path != self.server.expected_path:
            self._respond(404, "Not found", "That is not the callback route.", ok=False)
            return

        params = parse_qs(parsed.query)
        error = params.get("error", [None])[0]
        if error:
            self._respond(400, "Login failed", f"The provider returned: {error}", ok=False)
            self.server.failure = error
            self.server.done.set()
            return

        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        if not code:
            self._respond(400, "Login failed", "No authorization code in the redirect.", ok=False)
            return
        if self.server.expected_state is not None and state != self.server.expected_state:
            # A mismatched state is the CSRF case the parameter exists to catch.
            self._respond(400, "Login failed", "State mismatch - ignoring this redirect.", ok=False)
            return

        self._respond(200, "Signed in", "You can close this window and return to HX.", ok=True)
        self.server.result = CallbackResult(code=code, state=state)
        self.server.done.set()

    def _respond(self, status: int, title: str, body: str, *, ok: bool) -> None:
        payload = _page(title, body, ok=ok)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log - it would corrupt the TUI."""


class LoopbackCallback:
    """Owns the one-shot callback server.

    Start it *before* opening the browser, so a fast redirect cannot arrive at
    a closed port. Async callers use :meth:`start` / :meth:`aclose`; the sync
    CLI path uses it as a context manager.
    """

    def __init__(self, port: int, path: str, *, state: str | None = None) -> None:
        self.port = port
        self.path = path
        self._state = state
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._bind_error: OSError | None = None
        self._closing = threading.Event()
        self._closed = False

    @property
    def listening(self) -> bool:
        """False when the port could not be bound; only pasting will work then."""
        return self._server is not None

    def start(self) -> LoopbackCallback:
        """Bind and serve. Safe to call once; a failed bind is not an error."""
        try:
            server = HTTPServer((callback_host(), self.port), _Handler)
        except OSError as exc:
            # Not fatal: the paste path still works, and on a remote machine it
            # was always going to be the one that finished the login.
            self._bind_error = exc
            return self
        server.expected_path = self.path  # type: ignore[attr-defined]
        server.expected_state = self._state  # type: ignore[attr-defined]
        server.result = None  # type: ignore[attr-defined]
        server.failure = None  # type: ignore[attr-defined]
        server.done = threading.Event()  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __enter__(self) -> LoopbackCallback:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    async def aclose(self) -> None:
        """Shut down without stalling the caller's event loop.

        ``shutdown`` waits for the serving thread to notice, and the join waits
        again - a second or two of a frozen TUI at the end of every login, right
        where the user is looking for their new session.
        """
        await asyncio.to_thread(self.close)

    def close(self) -> None:
        """Stop serving and release the port. Safe to call twice.

        The flag is set at the end, not the start: a caller whose ``aclose`` was
        cancelled runs this again on its own thread to finish the job, and it
        can only do that while the work is still marked undone.
        """
        if self._closed:
            return
        # Release any thread parked in `wait` before tearing the server down;
        # `asyncio.to_thread` cannot be cancelled, so without this the thread
        # would sit on the event for the life of the process.
        self._closing.set()
        if self._server is not None:
            self._server.done.set()  # type: ignore[attr-defined]
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._closed = True

    async def wait(self) -> CallbackResult:
        """Block until the browser redirect lands.

        Cancellable: ``asyncio.CancelledError`` propagates so the caller can
        race this against a manual-paste prompt. When the port could not be
        bound this never resolves, leaving the paste to decide the outcome.
        """
        server = self._server
        if server is None:
            await asyncio.Event().wait()
            raise CallbackError(self._bind_message())  # pragma: no cover - unreachable

        await asyncio.to_thread(server.done.wait)  # type: ignore[attr-defined]
        if self._closing.is_set():
            raise CallbackError("Authorization was cancelled.")
        if server.failure:  # type: ignore[attr-defined]
            raise CallbackError(f"Authorization failed: {server.failure}")  # type: ignore[attr-defined]
        result = server.result  # type: ignore[attr-defined]
        if result is None:
            raise CallbackError("Authorization was cancelled.")
        return result  # type: ignore[no-any-return]

    def _bind_message(self) -> str:
        return (
            f"Could not listen on {callback_host()}:{self.port} ({self._bind_error}). "
            "Close whatever is using that port, or paste the redirect URL instead."
        )


def parse_redirect(value: str) -> CallbackResult:
    """Accept what a user might paste: a full redirect URL, ``code#state``, a
    query fragment, or a bare code."""
    text = value.strip()
    if not text:
        raise CallbackError("Nothing pasted.")

    if "://" in text:
        params = parse_qs(urlparse(text).query)
        code = params.get("code", [None])[0]
        if not code:
            raise CallbackError("That URL has no ?code= parameter.")
        return CallbackResult(code=code, state=params.get("state", [None])[0])

    if "code=" in text:
        params = parse_qs(text.lstrip("?"))
        code = params.get("code", [None])[0]
        if not code:
            raise CallbackError("Could not find a code in that text.")
        return CallbackResult(code=code, state=params.get("state", [None])[0])

    if "#" in text:
        code, _, state = text.partition("#")
        return CallbackResult(code=code, state=state or None)

    return CallbackResult(code=text)
