"""Tests for the Trust-On-First-Use TLS pinning connector.

These tests intentionally use a *real* local TLS server (not mocked
`resp.connection`) — that's how we discovered the v0.1.0 bug where
`resp.connection` was already `None` by the time user code reached the
`async with` block. The connector subclass approach captures the SPKI
inside `_wrap_create_connection`, so it's reliable regardless of
aiohttp's response-lifecycle quirks.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import ssl
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import aiohttp
import pytest
import pytest_socket
from cryptography import x509

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.x509.oid import NameOID

from custom_components.haggle.agl.pinning import (
    AGL_AUTH_HOST_NAME,
    AGL_BFF_HOST_NAME,
    HagglePinningConnector,
    _spki_hash_from_der,
)


def _make_cert_and_key(common_name: str) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Generate a self-signed cert + private key for a TLS test server."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _spki_hex(cert: x509.Certificate) -> str:
    """Compute the SHA-256 SPKI hash of a cert (matches what the connector emits)."""
    spki = cert.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(spki).hexdigest()


@asynccontextmanager
async def _tls_server(common_name: str = "localhost") -> AsyncIterator[tuple[int, str]]:
    """Spin up a one-shot localhost TLS server. Yield (port, expected_spki_hex).

    The server replies HTTP 200 with a tiny body to any request. The cert
    has CN=`common_name` and SAN=DNS:localhost, so an aiohttp client with
    SNI=`common_name` and `verify_ssl=False` (we're self-signed) connects
    cleanly.
    """
    # `pytest-homeassistant-custom-component` disables sockets globally; these
    # tests *must* hit a real local TLS port so re-enable for the duration of
    # the server context, then restore the default.
    pytest_socket.enable_socket()
    cert, key = _make_cert_and_key(common_name)
    expected_spki = _spki_hex(cert)

    with TemporaryDirectory() as tmp:
        cert_path = Path(tmp) / "cert.pem"
        key_path = Path(tmp) / "key.pem"
        cert_path.write_bytes(cert.public_bytes(Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n", b""):
                    break
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
            )
            await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
        port = server.sockets[0].getsockname()[1]
        try:
            yield port, expected_spki
        finally:
            server.close()
            await server.wait_closed()
            pytest_socket.disable_socket()


def test_pinned_host_constants_match_agl_endpoints() -> None:
    """If these change, every existing entry's pin becomes stale silently."""
    assert AGL_AUTH_HOST_NAME == "secure.agl.com.au"
    assert AGL_BFF_HOST_NAME == "api.platform.agl.com.au"


def test_spki_hash_matches_cryptography_round_trip() -> None:
    """`_spki_hash_from_der` must agree with a hand-computed SHA-256 of the SPKI."""
    cert, _ = _make_cert_and_key("test.example")
    der = cert.public_bytes(Encoding.DER)
    expected = _spki_hex(cert)

    assert _spki_hash_from_der(der) == expected
    assert len(_spki_hash_from_der(der)) == 64


@pytest.mark.enable_socket
class TestPinningConnectorAgainstRealTls:
    """Real TLS round-trip — would have caught the v0.1.0 lifecycle bug."""

    async def test_observed_dict_populated_on_first_connection(self) -> None:
        async with _tls_server() as (port, expected_spki):
            connector = HagglePinningConnector(ssl=False)
            async with (
                aiohttp.ClientSession(connector=connector) as s,
                s.get(f"https://localhost:{port}/") as r,
            ):
                assert r.status == 200

            assert "localhost" in connector.observed
            assert connector.observed["localhost"] == expected_spki
            assert len(connector.observed["localhost"]) == 64

    async def test_callback_fires_with_host_and_spki(self) -> None:
        observed: list[tuple[str, str]] = []

        def cb(host: str, spki: str) -> None:
            observed.append((host, spki))

        async with _tls_server() as (port, expected_spki):
            connector = HagglePinningConnector(ssl=False, on_new_connection=cb)
            async with (
                aiohttp.ClientSession(connector=connector) as s,
                s.get(f"https://localhost:{port}/") as r,
            ):
                assert r.status == 200

        assert observed == [("localhost", expected_spki)]

    async def test_callback_fires_per_new_connection(self) -> None:
        """Two requests with `Connection: close` → two new TLS handshakes → two captures."""
        observed: list[tuple[str, str]] = []

        def cb(host: str, spki: str) -> None:
            observed.append((host, spki))

        async with _tls_server() as (port, _expected):
            connector = HagglePinningConnector(ssl=False, on_new_connection=cb)
            async with aiohttp.ClientSession(connector=connector) as s:
                async with s.get(f"https://localhost:{port}/") as r:
                    assert r.status == 200
                async with s.get(f"https://localhost:{port}/") as r:
                    assert r.status == 200

        # Server sends Connection: close, so each request is a new TLS handshake.
        # Every NEW handshake should re-validate.
        assert len(observed) == 2
        assert all(host == "localhost" for host, _ in observed)
        assert observed[0] == observed[1]  # same cert → same SPKI

    async def test_callback_exception_does_not_break_request(self) -> None:
        """A throwing callback must not poison the data path."""

        def bad_cb(host: str, spki: str) -> None:
            raise RuntimeError("validator on fire")

        async with _tls_server() as (port, _expected):
            connector = HagglePinningConnector(ssl=False, on_new_connection=bad_cb)
            async with (
                aiohttp.ClientSession(connector=connector) as s,
                s.get(f"https://localhost:{port}/") as r,
            ):
                assert r.status == 200
                body = await r.text()

        assert body == "OK"
        # Capture still happened — exception was swallowed inside the connector.
        assert connector.observed.get("localhost") is not None

    async def test_no_callback_path_works(self) -> None:
        """Default (no callback) path must not crash."""
        async with _tls_server() as (port, expected_spki):
            connector = HagglePinningConnector(ssl=False)
            async with (
                aiohttp.ClientSession(connector=connector) as s,
                s.get(f"https://localhost:{port}/") as r,
            ):
                assert r.status == 200

        assert connector.observed["localhost"] == expected_spki


@pytest.mark.enable_socket
@pytest.mark.parametrize("requests", [1, 2, 3])
async def test_repeated_requests_always_have_an_observed_pin(requests: int) -> None:
    """Every request — first or N-th — must end with `observed[host]` populated.

    Regression for the v0.1.0 bug where the previous SPKI extraction read from
    `resp.connection` after the response was constructed; aiohttp had already
    released the connection by then, so `connection` was None and the
    integration silently shipped without a pin.
    """
    async with _tls_server() as (port, expected_spki):
        connector = HagglePinningConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as s:
            for _ in range(requests):
                async with s.get(f"https://localhost:{port}/") as r:
                    assert r.status == 200

        assert connector.observed.get("localhost") == expected_spki, (
            f"after {requests} request(s), expected SPKI {expected_spki[:12]} "
            f"but observed dict was {connector.observed}"
        )
