"""Tests for the Trust-On-First-Use SPKI pinning helpers."""

from __future__ import annotations

import datetime as dt
import hashlib
from unittest.mock import MagicMock

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.x509.oid import NameOID

from custom_components.haggle.agl.pinning import (
    AGL_AUTH_HOST_NAME,
    AGL_BFF_HOST_NAME,
    get_peer_spki_hash,
)


def _make_self_signed_cert() -> tuple[bytes, str]:
    """Generate a fresh self-signed cert and return (DER bytes, expected SPKI hex)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "haggle-test.invalid")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    der_cert = cert.public_bytes(Encoding.DER)
    spki_der = key.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    expected_spki = hashlib.sha256(spki_der).hexdigest()
    return der_cert, expected_spki


def _make_response_with_cert(der_cert: bytes | None) -> MagicMock:
    """Build a MagicMock aiohttp.ClientResponse that exposes the given cert."""
    ssl_object = MagicMock()
    ssl_object.getpeercert = MagicMock(return_value=der_cert)
    transport = MagicMock()
    transport.get_extra_info = MagicMock(return_value=ssl_object)
    connection = MagicMock()
    connection.transport = transport
    resp = MagicMock()
    resp.connection = connection
    return resp


# ---------------------------------------------------------------------------
# get_peer_spki_hash
# ---------------------------------------------------------------------------


class TestGetPeerSpkiHash:
    def test_extracts_sha256_of_spki_from_real_cert(self) -> None:
        """Hash a real DER cert and confirm it matches the cryptography-derived SPKI."""
        der_cert, expected_spki = _make_self_signed_cert()
        resp = _make_response_with_cert(der_cert)

        observed = get_peer_spki_hash(resp)

        assert observed == expected_spki
        assert observed is not None
        assert len(observed) == 64  # sha256 hex digest

    def test_returns_none_when_connection_is_none(self) -> None:
        resp = MagicMock()
        resp.connection = None

        assert get_peer_spki_hash(resp) is None

    def test_returns_none_when_transport_is_none(self) -> None:
        resp = MagicMock()
        resp.connection = MagicMock()
        resp.connection.transport = None

        assert get_peer_spki_hash(resp) is None

    def test_returns_none_when_ssl_object_is_none(self) -> None:
        resp = MagicMock()
        resp.connection = MagicMock()
        resp.connection.transport = MagicMock()
        resp.connection.transport.get_extra_info = MagicMock(return_value=None)

        assert get_peer_spki_hash(resp) is None

    def test_returns_none_when_cert_is_empty(self) -> None:
        resp = _make_response_with_cert(b"")

        assert get_peer_spki_hash(resp) is None


# ---------------------------------------------------------------------------
# Host names — guard against accidental rename
# ---------------------------------------------------------------------------


def test_pinned_host_constants_match_agl_endpoints() -> None:
    """If these change, every existing entry's pin becomes stale silently."""
    assert AGL_AUTH_HOST_NAME == "secure.agl.com.au"
    assert AGL_BFF_HOST_NAME == "api.platform.agl.com.au"
