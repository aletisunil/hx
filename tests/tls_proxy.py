"""The TLS-intercepting proxy a corporate network puts in front of the internet.

A security agent, a VPN client or an outbound proxy answers ``CONNECT`` and
then terminates TLS itself, with a certificate for the requested host signed
by its own CA. A client that does not trust that CA fails the handshake; one
that does sees the real server, because the proxy re-encrypts to it.

This is that, on localhost, with a throwaway CA from ``trustme``. Every host a
client reaches through it is recorded as ``intercepted`` (the client trusted
the CA and the bytes went upstream) or ``rejected`` (the client refused the
certificate), so a test can say not only that a request failed but why.

Run it by hand to reproduce a corporate network::

    uv run python tests/tls_proxy.py
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import threading
from dataclasses import dataclass, field
from pathlib import Path

import trustme
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtendedKeyUsageOID

HEADER_LIMIT = 64 * 1024


@dataclass
class InterceptingProxy:
    """Runs on its own thread and event loop, so a test can drive a
    subprocess or its own loop against it."""

    directory: Path
    ca: trustme.CA = field(default_factory=lambda: trustme.CA(organization_name="HX Test MITM"))
    require_client_cert: bool = False
    """Refuse a client that does not present a certificate from :attr:`ca` -
    the device certificate a zero-trust network asks every machine for."""
    wrong_purpose: bool = False
    """Sign through an issuing CA under :attr:`ca` whose extended key usage leaves
    out server authentication: the chain is trusted, and still refused, with
    "unsupported certificate purpose". A real inspection appliance did this."""
    events: list[tuple[str, str]] = field(default_factory=list)
    """``("intercepted" | "rejected", host)`` in the order they happened."""
    port: int = 0
    _contexts: dict[str, ssl.SSLContext] = field(default_factory=dict)
    _issuer: trustme.CA | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _server: asyncio.Server | None = None
    _thread: threading.Thread | None = None

    @property
    def intercepted(self) -> list[str]:
        return [host for verdict, host in self.events if verdict == "intercepted"]

    @property
    def rejected(self) -> list[str]:
        return [host for verdict, host in self.events if verdict == "rejected"]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ca_file(self) -> Path:
        path = self.directory / "intercepting-ca.pem"
        if not path.exists():
            self.ca.cert_pem.write_to_path(str(path))
        return path

    def client_cert(self) -> tuple[Path, Path]:
        """A device certificate this proxy accepts: ``(cert, key)`` PEM files."""
        issued = self.ca.issue_cert("hx-test-device")
        cert, key = self.directory / "device-cert.pem", self.directory / "device-key.pem"
        cert.write_bytes(b"".join(blob.bytes() for blob in issued.cert_chain_pems))
        issued.private_key_pem.write_to_path(str(key))
        return cert, key

    def start(self) -> InterceptingProxy:
        self.directory.mkdir(parents=True, exist_ok=True)
        _ = self.ca_file
        ready = threading.Event()

        def run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._server = loop.run_until_complete(
                asyncio.start_server(self._handle, "127.0.0.1", 0, limit=HEADER_LIMIT)
            )
            self.port = self._server.sockets[0].getsockname()[1]
            ready.set()
            loop.run_forever()
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

        self._thread = threading.Thread(target=run, name="intercepting-proxy", daemon=True)
        self._thread.start()
        if not ready.wait(10):
            raise RuntimeError("the intercepting proxy did not start")
        return self

    def stop(self) -> None:
        loop, server = self._loop, self._server
        if loop is None or server is None:
            return

        async def shut() -> None:
            server.close()
            # Open tunnels too: a client that keeps its connection alive would
            # otherwise hold them past the loop's end.
            tunnels = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tunnels:
                task.cancel()
            await asyncio.gather(*tunnels, return_exceptions=True)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2)

        asyncio.run_coroutine_threadsafe(shut(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(5)

    def __enter__(self) -> InterceptingProxy:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _context_for(self, host: str) -> ssl.SSLContext:
        context = self._contexts.get(host)
        if context is None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            issuer = self.ca
            if self.wrong_purpose:
                if self._issuer is None:
                    self._issuer = _client_only_child(self.ca)
                issuer = self._issuer
            issuer.issue_cert(host).configure_cert(context)
            if self.require_client_cert:
                context.verify_mode = ssl.CERT_REQUIRED
                self.ca.configure_trust(context)
            context.set_alpn_protocols(["http/1.1"])
            self._contexts[host] = context
        return context

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        method, _, rest = head.decode("latin-1").partition(" ")
        target = rest.split(" ", 1)[0]
        if method.upper() != "CONNECT":
            writer.write(b"HTTP/1.1 501 Not Implemented\r\nContent-Length: 0\r\n\r\n")
            await _close(writer)
            return
        host, _, port_text = target.rpartition(":")
        port = int(port_text or 443)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        try:
            await writer.start_tls(self._context_for(host))
            # Node verifies the server's certificate after the handshake has
            # finished and hangs up if it does not like it, so a completed
            # handshake proves nothing. A client that sends a request does.
            first = await asyncio.wait_for(reader.read(65536), 30)
        except (ssl.SSLError, ConnectionError, OSError, TimeoutError):
            first = b""
        if not first:
            # The client saw the CA and said no: the corporate-network failure.
            self.events.append(("rejected", host))
            await _close(writer)
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=_upstream_context(), server_hostname=host),
                15,
            )
        except (OSError, TimeoutError, ssl.SSLError):
            await _close(writer)
            return
        self.events.append(("intercepted", host))
        up_writer.write(first)
        await asyncio.gather(
            _pipe(reader, up_writer), _pipe(up_reader, writer), return_exceptions=True
        )


def _client_only_child(ca: trustme.CA) -> trustme.CA:
    """A CA under ``ca`` that may only vouch for TLS clients.

    trustme has no option for it, so its child CA is signed again with the
    extension added; the key, names and identifiers stay, and so does every
    certificate the child then issues.
    """
    child = ca.create_child_ca()
    cert = child._certificate
    builder = (
        x509.CertificateBuilder()
        .subject_name(cert.subject)
        .issuer_name(cert.issuer)
        .public_key(cert.public_key())
        .serial_number(cert.serial_number)
        .not_valid_before(cert.not_valid_before_utc)
        .not_valid_after(cert.not_valid_after_utc)
    )
    for extension in cert.extensions:
        builder = builder.add_extension(extension.value, extension.critical)
    builder = builder.add_extension(
        x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
    )
    child._certificate = builder.sign(ca._private_key, hashes.SHA256())
    return child


def _upstream_context() -> ssl.SSLContext:
    import truststore

    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.set_alpn_protocols(["http/1.1"])
    return context


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while chunk := await source.read(65536):
            sink.write(chunk)
            await sink.drain()
    except (ConnectionError, ssl.SSLError, OSError):
        pass
    finally:
        await _close(sink)


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 2)


if __name__ == "__main__":
    import tempfile
    import time

    with InterceptingProxy(Path(tempfile.mkdtemp(prefix="hx-mitm-"))) as proxy:
        print(f"HTTPS_PROXY={proxy.url}")
        print(f"HX_CA_BUNDLE={proxy.ca_file}")
        print("Ctrl+C to stop.")
        seen = 0
        with contextlib.suppress(KeyboardInterrupt):
            while True:
                time.sleep(0.5)
                for verdict, host in proxy.events[seen:]:
                    print(f"{verdict} {host}")
                seen = len(proxy.events)
