"""PKCE setup helpers — shared by the CLI (auth.py) and the web wizard (main.py)."""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from . import _bootstrap  # noqa: F401
from custom_components.haggle.agl.parser import parse_overview  # noqa: E402
from custom_components.haggle.agl.pinning import (  # noqa: E402
    AGL_AUTH_HOST_NAME,
    AGL_BFF_HOST_NAME,
    HagglePinningConnector,
)
from custom_components.haggle.const import (  # noqa: E402
    AGL_API_HOST,
    AGL_AUTH0_CLIENT,
    AGL_AUTH_HOST,
    AGL_CLIENT_FLAVOR,
    AGL_CLIENT_ID,
    AGL_OAUTH_AUDIENCE,
    AGL_OAUTH_SCOPE,
    AGL_REDIRECT_URI,
    AGL_USER_AGENT,
)


def gen_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def build_authorize_url(challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": AGL_CLIENT_ID,
        "redirect_uri": AGL_REDIRECT_URI,
        "scope": AGL_OAUTH_SCOPE,
        "audience": AGL_OAUTH_AUDIENCE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AGL_AUTH_HOST}/authorize?{urlencode(params)}"


def extract_code(callback_url: str, expected_state: str) -> str:
    parsed = urlparse(callback_url)
    params = parse_qs(parsed.query)
    if "error" in params:
        raise ValueError(f"Callback URL contains an error: {params['error'][0]}")
    state = (params.get("state") or [""])[0]
    if state != expected_state:
        raise ValueError("OAuth state mismatch — start over.")
    code = (params.get("code") or [""])[0]
    if not code:
        raise ValueError("Callback URL has no `code` parameter.")
    return code


async def exchange_code(code: str, verifier: str) -> tuple[str, str, str]:
    """Returns (access_token, refresh_token, secure.agl.com.au SPKI)."""
    payload = {
        "grant_type": "authorization_code",
        "client_id": AGL_CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": AGL_REDIRECT_URI,
    }
    headers = {
        "Client-Flavor": AGL_CLIENT_FLAVOR,
        "auth0-client": AGL_AUTH0_CLIENT,
        "User-Agent": AGL_USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    connector = HagglePinningConnector()
    async with (
        aiohttp.ClientSession(connector=connector) as session,
        session.post(f"{AGL_AUTH_HOST}/oauth/token", data=payload, headers=headers) as resp,
    ):
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(f"Token exchange failed HTTP {resp.status}: {text[:200]}")
        body: dict[str, Any] = await resp.json()
    access_token = body.get("access_token", "")
    refresh_token = body.get("refresh_token", "")
    if not access_token or not refresh_token:
        raise RuntimeError("Token response missing access_token/refresh_token")
    return access_token, refresh_token, connector.observed.get(AGL_AUTH_HOST_NAME, "")


async def fetch_contracts(access_token: str) -> tuple[list[Any], str]:
    """Returns (contracts, api.platform.agl.com.au SPKI)."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Client-Flavor": AGL_CLIENT_FLAVOR,
        "User-Agent": AGL_USER_AGENT,
        "Accept": "*/*",
    }
    connector = HagglePinningConnector()
    async with (
        aiohttp.ClientSession(connector=connector) as session,
        session.get(
            f"{AGL_API_HOST}/mobile/bff/api/v3/overview", headers=headers
        ) as resp,
    ):
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(f"/v3/overview failed HTTP {resp.status}: {text[:200]}")
        data = await resp.json(content_type=None)
    return parse_overview(data), connector.observed.get(AGL_BFF_HOST_NAME, "")
