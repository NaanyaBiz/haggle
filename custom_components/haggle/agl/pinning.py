"""Trust-On-First-Use TLS certificate pinning for AGL endpoints.

The integration captures the SHA-256 SPKI (SubjectPublicKeyInfo) hash of each
AGL host's leaf certificate during the initial PKCE config flow and persists
both hashes to the config entry. Every new TLS connection to those hosts
observes the live SPKI; the optional `on_new_connection` callback lets the
caller compare against the stored pin and raise on mismatch.

## Why a connector subclass

aiohttp releases the Connection back to its pool the moment a response object
is constructed for user code, so `resp.connection` (and `resp._protocol.transport`)
are already `None` by the time `async with session.get(...) as resp:` enters.
The only reliable hook for SSL introspection is the *connector's*
`_wrap_create_connection` override, which runs synchronously after the TLS
handshake completes and returns `(transport, protocol)` to the connector pool.
TraceConfig events are also unsuitable: `TraceConnectionCreateEndParams` has
no fields and exposes neither the protocol nor the transport.

## Design

- `HagglePinningConnector` is a drop-in `aiohttp.TCPConnector` subclass.
- For every NEW connection it makes, it extracts the leaf-cert SPKI hash and
  stores it on `self.observed[host]`. Connection reuse (keep-alive) is by
  definition the same TLS session, so no re-validation is needed.
- An optional `on_new_connection(host, spki)` callback is invoked synchronously
  for each new connection so callers can validate against a stored pin.
- Mismatch handling stays warn-only at the call-site (per the security review):
  the callback logs a warning + emits an HA persistent notification but does
  NOT raise, so legitimate AGL cert rotations cannot brick HACS users.

## Why warn-only

Hard-coding a SPKI in `const.py` would brick every install whenever AGL
rotates. TOFU lets each install pin what it observes at install time — when
the user has already verified the AGL hostname in their browser (PKCE happens
browser-side, with system trust + lock indicator). The HA-side pin then locks
down post-install requests. Re-pin via the standard HA Reconfigure flow.

## First-install caveat

A LAN MITM during the initial PKCE flow could pin the attacker's certificate.
PKCE happens in the user's browser (system trust + visible lock indicator),
so this requires compromising both the browser and the HA host simultaneously.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

# Hosts the integration knows how to pin. Anything else is invisible to the
# validator and simply not pinned — but the OAuth state nonce + PKCE verifier
# already block redirect-to-attacker scenarios.
AGL_AUTH_HOST_NAME = "secure.agl.com.au"
AGL_BFF_HOST_NAME = "api.platform.agl.com.au"


def _spki_hash_from_der(der_cert: bytes) -> str:
    """Return SHA-256 hex digest of the SubjectPublicKeyInfo of a DER cert."""
    cert = x509.load_der_x509_certificate(der_cert)
    spki_der = cert.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(spki_der).hexdigest()


class HagglePinningConnector(aiohttp.TCPConnector):
    """`TCPConnector` that captures the leaf-cert SPKI of each new connection.

    The captured hash is stored on `self.observed[host]` keyed by the SNI
    server name. If `on_new_connection` is set, it is invoked with
    `(host, spki_hex)` for every new connection — the integration uses this
    to validate against the persisted Trust-On-First-Use pin.

    Connection reuse (HTTP keep-alive) does not trigger the callback, since
    by definition it is the same TLS session.
    """

    def __init__(
        self,
        *args: Any,
        on_new_connection: Callable[[str, str], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.observed: dict[str, str] = {}
        self.on_new_connection = on_new_connection

    async def _wrap_create_connection(  # type: ignore[no-untyped-def]
        self, *args, **kwargs
    ):
        """Override: after a new TCP+TLS connection is established, capture the SPKI."""
        transport, protocol = await super()._wrap_create_connection(*args, **kwargs)
        try:
            ssl_obj = transport.get_extra_info("ssl_object")
            if ssl_obj is not None:
                host = ssl_obj.server_hostname
                der = ssl_obj.getpeercert(binary_form=True)
                if host and der:
                    spki = _spki_hash_from_der(der)
                    self.observed[host] = spki
                    if self.on_new_connection is not None:
                        self.on_new_connection(host, spki)
        except Exception:
            # SPKI introspection must never fail the connection. Pinning
            # degrades to no-op rather than blocking AGL traffic.
            _LOGGER.debug("SPKI capture failed", exc_info=True)
        return transport, protocol
