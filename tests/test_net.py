"""Trust store selection and the one-line error text built on top of it."""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx
import pytest

from hx import net


@pytest.fixture(autouse=True)
def _clean_tls_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*net.CA_BUNDLE_VARS, net.NO_VERIFY_VAR):
        monkeypatch.delenv(name, raising=False)
    net.ssl_verify.cache_clear()


def _bundle(tmp_path: Path, name: str = "corp-ca.pem") -> Path:
    # Any parseable PEM will do: what is under test is which file we pick, not
    # whether that CA signs anything.
    import certifi

    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(Path(certifi.where()).read_text())
    return path


def test_hx_ca_bundle_wins_over_the_generic_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ours, theirs = _bundle(tmp_path), _bundle(tmp_path, "other-ca.pem")
    monkeypatch.setenv("SSL_CERT_FILE", str(theirs))
    monkeypatch.setenv("HX_CA_BUNDLE", str(ours))
    assert net.ca_bundle() == ours


def test_a_bundle_that_is_not_there_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale ``SSL_CERT_FILE`` should not turn every request into a crash."""
    monkeypatch.setenv("HX_CA_BUNDLE", "/nowhere/ca.pem")
    assert net.ca_bundle() is None
    assert net.ssl_verify() is not False


def test_the_named_bundle_becomes_the_verify_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HX_CA_BUNDLE", str(_bundle(tmp_path)))
    context = net.ssl_verify()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED


def test_without_configuration_we_verify_against_the_system_store() -> None:
    context = net.ssl_verify()
    assert isinstance(context, ssl.SSLContext)
    assert type(context).__module__.startswith("truststore")


def test_verification_can_be_turned_off_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(net.NO_VERIFY_VAR, "1")
    assert net.ssl_verify() is False
    notice = net.tls_notice()
    assert notice is not None and "not being verified" in notice


def test_stock_tls_gets_no_startup_notice() -> None:
    assert net.tls_notice() is None


def _connect_error(cause: Exception) -> httpx.ConnectError:
    error = httpx.ConnectError("connection failed")
    error.__cause__ = cause
    error.request = httpx.Request("GET", "https://openrouter.ai/api/v1/models")
    return error


def test_a_buried_certificate_failure_is_described_with_its_fix() -> None:
    """The useful part is three frames down inside an httpx ConnectError."""
    verify = ssl.SSLCertVerificationError(1, "certificate verify failed")
    verify.verify_message = "self signed certificate in certificate chain"
    described = net.describe(_connect_error(verify))
    assert "self signed certificate in certificate chain" in described
    assert "HX_CA_BUNDLE" in described


def test_an_unreachable_host_is_named() -> None:
    described = net.describe(_connect_error(OSError("nodename nor servname provided")))
    assert "openrouter.ai" in described


def test_a_timeout_reads_as_a_timeout() -> None:
    timeout = httpx.ConnectTimeout("timed out")
    timeout.request = httpx.Request("GET", "https://openrouter.ai/api/v1/models")
    assert net.describe(timeout).startswith("Timed out talking to openrouter.ai")


def test_anything_else_still_fits_on_one_line() -> None:
    described = net.describe(RuntimeError("boom\n  with a stack-shaped\n  second half"))
    assert described == "RuntimeError: boom with a stack-shaped second half"
    assert "\n" not in described


async def test_async_client_carries_the_chosen_trust_store(tmp_path: Path) -> None:
    client = net.async_client(timeout=1.0)
    async with client:
        assert client.timeout.connect == 1.0
