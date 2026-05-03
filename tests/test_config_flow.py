"""Tests for the haggle config flow (PKCE OAuth2 path)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.haggle.agl.client import AGLAuthError, AGLError
from custom_components.haggle.agl.models import Contract
from custom_components.haggle.config_flow import CALLBACK_URL_FIELD
from custom_components.haggle.const import (
    CONF_CONTRACT_NUMBER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_CONTRACT = Contract(
    contract_number="9999999999",
    account_number="1234567890",
    address="1 Sample Street SUBURB QLD 4000",
    fuel_type="electricityContract",
    status="active",
)


def _make_callback_url(authorize_url: str, code: str = "auth_code_123") -> str:
    """Build a fake callback URL with the same state as the authorize URL."""
    qs = parse_qs(urlparse(authorize_url).query)
    state = (qs.get("state") or ["state"])[0]
    redirect_uri = (qs.get("redirect_uri") or ["https://example.com/callback"])[0]
    return f"{redirect_uri}?code={code}&state={state}"


async def test_user_step_shows_pkce_form(hass: HomeAssistant) -> None:
    """User step renders the PKCE form with an authorize_url placeholder."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert "authorize_url" in result["description_placeholders"]


async def test_user_flow_single_contract_creates_entry(hass: HomeAssistant) -> None:
    """Full PKCE flow with one contract creates an entry directly."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    authorize_url: str = result["description_placeholders"]["authorize_url"]
    callback_url = _make_callback_url(authorize_url)

    with (
        patch(
            "custom_components.haggle.config_flow._exchange_code",
            new_callable=AsyncMock,
            return_value=("access_tok", "refresh_tok", "deadbeef" * 8),
        ),
        patch(
            "custom_components.haggle.config_flow._fetch_contracts",
            new_callable=AsyncMock,
            return_value=([_CONTRACT], "cafef00d" * 8),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CALLBACK_URL_FIELD: callback_url},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh_tok"
    assert result["data"][CONF_CONTRACT_NUMBER] == "9999999999"


async def test_user_flow_bad_state_shows_error(hass: HomeAssistant) -> None:
    """Callback URL with wrong state shows invalid_auth error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    bad_callback = (
        "https://secure.agl.com.au/ios/au.com.agl.mobile/callback?code=abc&state=WRONG"
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CALLBACK_URL_FIELD: bad_callback},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_user_flow_exchange_failure_shows_error(hass: HomeAssistant) -> None:
    """Token exchange failure shows invalid_auth error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    authorize_url: str = result["description_placeholders"]["authorize_url"]
    callback_url = _make_callback_url(authorize_url)

    with patch(
        "custom_components.haggle.config_flow._exchange_code",
        new_callable=AsyncMock,
        side_effect=AGLAuthError("rejected"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CALLBACK_URL_FIELD: callback_url},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_fetch_contracts_failure_shows_cannot_connect(
    hass: HomeAssistant,
) -> None:
    """When _fetch_contracts raises, select_contract shows cannot_connect error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    authorize_url: str = result["description_placeholders"]["authorize_url"]
    callback_url = _make_callback_url(authorize_url)

    with (
        patch(
            "custom_components.haggle.config_flow._exchange_code",
            new_callable=AsyncMock,
            return_value=("access_tok", "refresh_tok", "deadbeef" * 8),
        ),
        patch(
            "custom_components.haggle.config_flow._fetch_contracts",
            new_callable=AsyncMock,
            side_effect=AGLError("network error"),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CALLBACK_URL_FIELD: callback_url},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_contract"
    assert result["errors"]["base"] == "cannot_connect"


async def test_unique_id_fallback_hashes_refresh_token(hass: HomeAssistant) -> None:
    """When _fetch_contracts returns nothing, unique_id must be a hash, not a token prefix.

    SAST-001 / SEC-001: HA's entity registry is plaintext JSON on disk; a leaked
    refresh-token prefix from there could be correlated against captured token
    material. Hashing makes the on-disk identifier one-way.
    """
    import hashlib

    refresh_token = "v1.MdLHBM9JNbgB4ABUpW5K_FAKE_token_for_test_only"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    authorize_url: str = result["description_placeholders"]["authorize_url"]
    callback_url = _make_callback_url(authorize_url)

    with (
        patch(
            "custom_components.haggle.config_flow._exchange_code",
            new_callable=AsyncMock,
            return_value=("access_tok", refresh_token, "deadbeef" * 8),
        ),
        patch(
            "custom_components.haggle.config_flow._fetch_contracts",
            new_callable=AsyncMock,
            return_value=([], "cafef00d" * 8),  # no contracts → fallback path
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CALLBACK_URL_FIELD: callback_url},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    expected_hash = hashlib.sha256(refresh_token.encode()).hexdigest()[:16]

    assert entry.unique_id == expected_hash
    assert entry.unique_id != refresh_token[:16]
    assert refresh_token[:8] not in (entry.unique_id or "")


async def test_user_flow_multiple_contracts_shows_selector(hass: HomeAssistant) -> None:
    """Two discovered contracts show the select_contract form."""
    second = Contract(
        contract_number="1111111111",
        account_number="1234567890",
        address="1 Sample Street SUBURB QLD 4000",
        fuel_type="gasContract",
        status="active",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    authorize_url: str = result["description_placeholders"]["authorize_url"]
    callback_url = _make_callback_url(authorize_url)

    with (
        patch(
            "custom_components.haggle.config_flow._exchange_code",
            new_callable=AsyncMock,
            return_value=("access_tok", "refresh_tok", "deadbeef" * 8),
        ),
        patch(
            "custom_components.haggle.config_flow._fetch_contracts",
            new_callable=AsyncMock,
            return_value=([_CONTRACT, second], "cafef00d" * 8),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CALLBACK_URL_FIELD: callback_url},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_contract"
