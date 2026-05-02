"""Tests for constants — currently focused on the auth0-client identity blob."""

from __future__ import annotations

import base64
import json

from custom_components.haggle.const import AGL_AUTH0_CLIENT


def test_auth0_client_decodes_to_expected_sdk_identity() -> None:
    """Decoding AGL_AUTH0_CLIENT yields the AGL iOS SDK identity Auth0 expects.

    Auth0 doesn't byte-validate this header, but the JSON shape is required:
    `name`, `version`, and `env` keys must be present so a future refactor
    can't quietly mangle the encoding (e.g. SAST-006 had two divergent base64
    encodings of the same JSON in const.py and client.py — now consolidated
    here).
    """
    # Pad to a multiple of 4 (AGL ships unpadded base64).
    padded = AGL_AUTH0_CLIENT + "=" * (-len(AGL_AUTH0_CLIENT) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))

    assert payload["name"] == "Auth0.swift"
    assert payload["version"].startswith("2.")
    assert "env" in payload
    assert "iOS" in payload["env"]
