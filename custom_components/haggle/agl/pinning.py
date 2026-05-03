"""Trust-On-First-Use TLS certificate pinning for AGL endpoints.

The integration captures the SHA-256 SPKI (SubjectPublicKeyInfo) hash of each
AGL host's leaf certificate during the initial PKCE config flow and persists
both hashes to the config entry. Every subsequent token-refresh and BFF
request is observed and the live SPKI is compared against the stored pin.

Mismatch handling is a soft warn-on-mismatch: log + HA persistent notification.
The integration does not block the request, so a legitimate AGL cert rotation
does not brick HACS users — they re-pin via the standard Reconfigure flow.
A strict-reject mode is left for v0.2.x once we have a feel for AGL's rotation
cadence.

First-install caveat: PKCE happens in the user's browser (system trust store +
visible lock indicator), so a LAN MITM at HA does not necessarily compromise
the auth bootstrap. The integration cannot defend against MITM on first auth
without pre-shipping a cert hash, which would tightly couple every release to
AGL's cert rotation. See SECURITY.md for the full threat model.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

if TYPE_CHECKING:
    import aiohttp

_LOGGER = logging.getLogger(__name__)

# Hosts the integration knows how to pin. Anything else (e.g. a MITM-injected
# redirect to attacker.example) is invisible to the validator and simply not
# pinned — but the OAuth state nonce + PKCE verifier already block that path.
AGL_AUTH_HOST_NAME = "secure.agl.com.au"
AGL_BFF_HOST_NAME = "api.platform.agl.com.au"


def get_peer_spki_hash(resp: aiohttp.ClientResponse) -> str | None:
    """Return SHA-256 hex digest of the response's leaf-cert SPKI, or None.

    Used both during config-flow capture and at every subsequent request site.
    Returns None if the response was not over TLS or if the platform doesn't
    expose the underlying SSL object — in which case validation degrades to a
    no-op (better than failing closed for a transient introspection issue).
    """
    conn = resp.connection
    if conn is None:
        return None
    transport = conn.transport
    if transport is None:
        return None
    ssl_object = transport.get_extra_info("ssl_object")
    if ssl_object is None:
        return None
    der_cert = ssl_object.getpeercert(binary_form=True)
    if not der_cert:
        return None
    cert = x509.load_der_x509_certificate(der_cert)
    spki_der = cert.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(spki_der).hexdigest()
