"""Tests for the haggle config flow (PKCE OAuth2 path)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant import config_entries
from homeassistant.components import persistent_notification as _pn
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haggle.agl.client import AGLAuthError, AGLError
from custom_components.haggle.agl.models import Contract
from custom_components.haggle.agl.pinning import AGL_AUTH_HOST_NAME, AGL_BFF_HOST_NAME
from custom_components.haggle.config_flow import CALLBACK_URL_FIELD
from custom_components.haggle.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_PINNED_SPKI_AUTH,
    CONF_PINNED_SPKI_BFF,
    CONF_REFRESH_TOKEN,
    CONF_SOLAR_HEAL,
    CONF_SOLAR_STALL_SPANS,
    DOMAIN,
    PIN_MISMATCH_NOTIFICATION_ID,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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
        # Short-circuit `async_setup_entry` — pytest-HA 0.13.325+ trips on
        # any socket use during teardown, and CREATE_ENTRY triggers the
        # coordinator's first refresh against the real AGL API.
        patch(
            "custom_components.haggle.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CALLBACK_URL_FIELD: callback_url},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh_tok"
    assert result["data"][CONF_CONTRACT_NUMBER] == "9999999999"
    # #26: only the refresh token should land on disk. The short-lived
    # access_token has no business in entry.data.
    assert "access_token" not in result["data"]
    assert "access_token_expiry" not in result["data"]


async def test_pkce_verifier_cleared_after_successful_exchange(
    hass: HomeAssistant,
) -> None:
    """#27: PKCE verifier+challenge are zeroed once the exchange consumes them."""
    flows = hass.config_entries.flow
    result = await flows.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    flow_id = result["flow_id"]
    flow = next(f for f in flows._progress.values() if f.flow_id == flow_id)

    authorize_url: str = result["description_placeholders"]["authorize_url"]
    assert flow._pkce_verifier  # populated on entry to async_step_user
    assert flow._pkce_challenge

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
        patch(
            "custom_components.haggle.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await flows.async_configure(
            flow_id,
            user_input={CALLBACK_URL_FIELD: _make_callback_url(authorize_url)},
        )

    # Both PKCE secrets must be cleared once consumed.
    assert flow._pkce_verifier == ""
    assert flow._pkce_challenge == ""


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
        patch(
            "custom_components.haggle.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
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
    """Two discovered ELECTRICITY contracts show the select_contract form.

    Deliberately two electricity contracts: pairing one with a gas contract
    would now exercise the #260 filter (which reduces the pair to one and
    auto-selects) rather than the multi-contract selector this test is for.
    """
    second = Contract(
        contract_number="1111111111",
        account_number="1234567890",
        address="2 Sample Street SUBURB QLD 4000",
        fuel_type="electricityContract",
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


def _mock_token_session(
    body: object = None, *, status: int = 200, json_exc: Exception | None = None
) -> MagicMock:
    """Mock the short-lived ClientSession _exchange_code builds internally."""
    resp = AsyncMock()
    resp.status = status
    resp.ok = 200 <= status < 400
    resp.json = (
        AsyncMock(side_effect=json_exc)
        if json_exc is not None
        else AsyncMock(return_value=body)
    )
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


class TestExchangeCodeMalformedResponses:
    """#243 — _exchange_code must not let raw exceptions past its boundary.

    async_step_exchange catches only AGLAuthError and
    (AGLError, aiohttp.ClientError, TimeoutError). Anything else aborts the
    config flow with an untranslated "Unknown error" instead of the intended
    `cannot_connect` / `invalid_auth` form. These paths were previously
    untested — every other test in this file mocks _exchange_code out.
    """

    @staticmethod
    async def _exchange(**kw: object) -> tuple[str, str, str]:
        from custom_components.haggle.config_flow import _exchange_code

        with (
            patch(
                "custom_components.haggle.config_flow.aiohttp.ClientSession",
                return_value=_mock_token_session(**kw),  # type: ignore[arg-type]
            ),
            patch("custom_components.haggle.config_flow.HagglePinningConnector"),
        ):
            return await _exchange_code("auth_code", "verifier")

    @pytest.mark.parametrize("body", [None, [], "a string", 7])
    async def test_non_object_body_raises_agl_error(self, body: object) -> None:
        with pytest.raises(AGLError):
            await self._exchange(body=body)

    async def test_undecodable_json_raises_agl_error(self) -> None:
        with pytest.raises(AGLError):
            await self._exchange(json_exc=json.JSONDecodeError("bad", "doc", 0))

    async def test_mistyped_token_fields_raise_agl_error_not_auth(self) -> None:
        """Shape faults map to cannot_connect, not invalid_auth.

        Review finding (two independent reviewers): this originally raised
        AGLAuthError, telling the user their credentials were wrong for what
        is a server-response problem — and contradicting async_force_refresh's
        AGLTransportError treatment of the identical condition.
        """
        with pytest.raises(AGLError) as exc:
            await self._exchange(body={"access_token": 1, "refresh_token": ["r"]})
        assert not isinstance(exc.value, AGLAuthError)

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"access_token": "a"},
            {"access_token": "", "refresh_token": ""},
            {"access_token": "a", "refresh_token": ""},
            {"access_token": "a", "refresh_token": " "},
            {"access_token": "a", "refresh_token": "r\n"},
        ],
    )
    async def test_missing_or_blank_tokens_raise_agl_error_not_auth(
        self, body: dict
    ) -> None:
        """Missing/blank tokens are the same schema-fault family (Codex pass 2).

        This branch sat one line below the mistyped-field fix and still
        raised AGLAuthError — surfacing invalid_auth and telling the user to
        re-authenticate for an upstream fault retrying might fix.
        """
        with pytest.raises(AGLError) as exc:
            await self._exchange(body=body)
        assert not isinstance(exc.value, AGLAuthError)

    async def test_401_still_maps_to_auth_error(self) -> None:
        """The genuine-auth-failure path is unchanged by the malformed-200 work."""
        with pytest.raises(AGLAuthError):
            await self._exchange(body={"error": "invalid_grant"}, status=401)

    async def test_malformed_body_surfaces_cannot_connect_not_unknown_error(
        self, hass: HomeAssistant
    ) -> None:
        """End-to-end: the user sees the translated error, not "Unknown error"."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        callback_url = _make_callback_url(
            result["description_placeholders"]["authorize_url"]
        )
        with (
            patch(
                "custom_components.haggle.config_flow.aiohttp.ClientSession",
                return_value=_mock_token_session(body=None),
            ),
            patch("custom_components.haggle.config_flow.HagglePinningConnector"),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={CALLBACK_URL_FIELD: callback_url}
            )

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}


def _gas(number: str = "1111111111") -> Contract:
    return Contract(
        contract_number=number,
        account_number="1234567890",
        address="1 Sample Street SUBURB QLD 4000",
        fuel_type="gasContract",
        status="active",
    )


async def _run_discovery(hass: HomeAssistant, contracts: list[Contract]) -> Any:
    """Drive the flow to the point where contract selection happens."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    callback_url = _make_callback_url(
        result["description_placeholders"]["authorize_url"]
    )
    with (
        patch(
            "custom_components.haggle.config_flow._exchange_code",
            new_callable=AsyncMock,
            return_value=("access_tok", "refresh_tok", "deadbeef" * 8),
        ),
        patch(
            "custom_components.haggle.config_flow._fetch_contracts",
            new_callable=AsyncMock,
            return_value=(contracts, "cafef00d" * 8),
        ),
    ):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CALLBACK_URL_FIELD: callback_url}
        )


async def test_gas_only_account_aborts_instead_of_autoselecting(
    hass: HomeAssistant,
) -> None:
    """A gas-only account aborts rather than silently creating a dead entry (#260).

    Regression for the single-contract fast path: it took no fuel type into
    account, so a gas-only account had its contract auto-selected with no user
    choice, and every later call hit /Electricity/{gasContractNumber}.
    """
    result = await _run_discovery(hass, [_gas()])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_electricity_contract"


async def test_gas_contract_filtered_out_of_selector(hass: HomeAssistant) -> None:
    """A mixed account auto-selects the electricity contract, never offering gas."""
    result = await _run_discovery(hass, [_CONTRACT, _gas()])

    # Only one serviceable contract remains, so the fast path takes it.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CONTRACT_NUMBER] == _CONTRACT.contract_number


async def test_unknown_fuel_type_still_selectable(hass: HomeAssistant) -> None:
    """An unreported fuel type must NOT lock a user out — the filter fails open.

    Locking out a working electricity install because AGL renamed or dropped
    the `type` field would be a worse failure than the one #260 fixes, so
    only a positively-identified non-electricity fuel is excluded.
    """
    unknown = Contract(
        contract_number="2222222222",
        account_number="1234567890",
        address="1 Sample Street SUBURB QLD 4000",
        fuel_type="",
        status="active",
    )
    result = await _run_discovery(hass, [unknown])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CONTRACT_NUMBER] == "2222222222"


async def test_unrecognized_nonempty_fuel_still_selectable(
    hass: HomeAssistant,
) -> None:
    """A renamed/unknown NONEMPTY fuel type must also fail open (Codex, PR #261).

    The first cut required the literal "electricity" substring, which kept
    empty types but silently excluded any unknown nonempty wording (e.g. a
    renamed `powerContract`) — on a single-contract account that aborted
    setup entirely. The filter is a denylist of known-unservable fuels, not
    an allowlist of known-good ones.
    """
    renamed = Contract(
        contract_number="3333333333",
        account_number="1234567890",
        address="1 Sample Street SUBURB QLD 4000",
        fuel_type="powerContract",
        status="active",
    )
    result = await _run_discovery(hass, [renamed])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CONTRACT_NUMBER] == "3333333333"


async def test_serviceable_filter_is_case_insensitive(hass: HomeAssistant) -> None:
    """Fuel-type matching tolerates casing/wording drift on AGL's side."""
    from custom_components.haggle.config_flow import _serviceable_contracts

    variants = [
        Contract("1", "a", "", "ElectricityContract", "active"),
        Contract("2", "a", "", "electricity", "active"),
        Contract("3", "a", "", "gasContract", "active"),
        Contract("4", "a", "", "GASCONTRACT", "active"),
        Contract("5", "a", "", "", "active"),
    ]
    kept = [c.contract_number for c in _serviceable_contracts(variants)]

    assert kept == ["1", "2", "5"]


async def test_options_flow_toggles_solar_writes(hass: HomeAssistant) -> None:
    """The options flow exposes and persists the solar-writes toggle (CO-10.3)."""
    from custom_components.haggle.const import OPT_SOLAR_STATISTICS_ENABLED

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONTRACT_NUMBER: "9999999999", CONF_REFRESH_TOKEN: "v1.t"},
        unique_id="opt-test",
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_SOLAR_STATISTICS_ENABLED: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_SOLAR_STATISTICS_ENABLED] is False


async def test_options_flow_defaults_to_enabled(hass: HomeAssistant) -> None:
    """The toggle defaults to True — solar writes on unless the user opts out."""
    from custom_components.haggle.const import OPT_SOLAR_STATISTICS_ENABLED

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONTRACT_NUMBER: "9999999999", CONF_REFRESH_TOKEN: "v1.t"},
        unique_id="opt-default",
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    key = next(k for k in schema if k.schema == OPT_SOLAR_STATISTICS_ENABLED)
    assert key.default() is True


async def test_options_flow_poll_interval_defaults_to_24(hass: HomeAssistant) -> None:
    """#228: the poll-interval field defaults to 24h — unchanged cadence
    unless the user opts to throttle it back."""
    from custom_components.haggle.const import (
        DEFAULT_POLL_INTERVAL_HOURS,
        OPT_POLL_INTERVAL_HOURS,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONTRACT_NUMBER: "9999999999", CONF_REFRESH_TOKEN: "v1.t"},
        unique_id="opt-poll-default",
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    key = next(k for k in schema if k.schema == OPT_POLL_INTERVAL_HOURS)
    assert key.default() == DEFAULT_POLL_INTERVAL_HOURS


async def test_options_flow_sets_poll_interval(hass: HomeAssistant) -> None:
    """A valid in-range poll interval is accepted and persisted."""
    from custom_components.haggle.const import (
        OPT_POLL_INTERVAL_HOURS,
        OPT_SOLAR_STATISTICS_ENABLED,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONTRACT_NUMBER: "9999999999", CONF_REFRESH_TOKEN: "v1.t"},
        unique_id="opt-poll-set",
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {OPT_SOLAR_STATISTICS_ENABLED: True, OPT_POLL_INTERVAL_HOURS: 72},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_POLL_INTERVAL_HOURS] == 72


@pytest.mark.parametrize("bad_value", [1, 23, 169, 999])
async def test_options_flow_rejects_out_of_range_poll_interval(
    hass: HomeAssistant, bad_value: int
) -> None:
    """The 24h floor and 168h ceiling are enforced at the options-flow layer
    (belt-and-braces with the coordinator-side clamp — see const.py)."""
    import voluptuous as vol

    from custom_components.haggle.const import (
        OPT_POLL_INTERVAL_HOURS,
        OPT_SOLAR_STATISTICS_ENABLED,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONTRACT_NUMBER: "9999999999", CONF_REFRESH_TOKEN: "v1.t"},
        unique_id=f"opt-poll-reject-{bad_value}",
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(vol.Invalid):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {OPT_SOLAR_STATISTICS_ENABLED: True, OPT_POLL_INTERVAL_HOURS: bad_value},
        )


# -----------------------------------------------------------------------
# Reauth and Reconfigure (#275): repair an existing entry
# -----------------------------------------------------------------------

_OLD_AUTH_SPKI = "a" * 64  # pin stored at initial setup
_OLD_BFF_SPKI = "b" * 64
_NEW_AUTH_SPKI = "c" * 64  # pin captured on the fresh login
_NEW_BFF_SPKI = "d" * 64


def _repair_entry(
    hass: Any,
    *,
    contract: str = "9999999999",
    account: str = "1234567890",
    uid: str | None = None,
    **extra_data: Any,
) -> MockConfigEntry:
    """Entry pre-loaded with stored TOFU pins, ready for reauth/reconfigure."""
    data: dict[str, Any] = {
        CONF_REFRESH_TOKEN: "v1.old_token",
        CONF_CONTRACT_NUMBER: contract,
        CONF_ACCOUNT_NUMBER: account,
        CONF_PINNED_SPKI_AUTH: _OLD_AUTH_SPKI,
        CONF_PINNED_SPKI_BFF: _OLD_BFF_SPKI,
        **extra_data,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="1 Sample St",
        unique_id=uid or f"{account}_{contract}",
        data=data,
    )
    entry.add_to_hass(hass)
    return entry


def _matching_contract(
    contract: str = "9999999999",
    account: str = "1234567890",
    fuel: str = "electricityContract",
) -> Contract:
    """Contract that matches the placeholder identifiers in `_repair_entry`."""
    return Contract(
        contract_number=contract,
        account_number=account,
        address="1 Sample Street SUBURB QLD 4000",
        fuel_type=fuel,
        status="active",
    )


def _notification_ids(hass: Any) -> set[str]:
    """Return the set of current persistent notification ids."""
    return set(_pn._async_get_or_create_notifications(hass).keys())


def _seed_pin_notices(hass: Any) -> None:
    """Pre-create both pin-mismatch notifications so dismiss tests have something to dismiss."""
    for host in (AGL_AUTH_HOST_NAME, AGL_BFF_HOST_NAME):
        _pn.async_create(
            hass,
            "mismatch",
            notification_id=PIN_MISMATCH_NOTIFICATION_ID.format(host=host),
        )


async def _run_repair(
    hass: Any,
    entry: MockConfigEntry,
    *,
    source: str,
    contracts: list[Contract] | None = None,
    auth_spki: str = _NEW_AUTH_SPKI,
    bff_spki: str = _NEW_BFF_SPKI,
    setup_mock: AsyncMock | None = None,
    between: Callable[[], None] | None = None,
) -> Any:
    """Initiate and complete a reauth or reconfigure flow against `entry`.

    Returns the final flow result (ABORT or FORM on error). Pass `setup_mock`
    to observe whether the entry was reloaded. `between` runs after the form is
    shown and before the callback is submitted — i.e. while the flow is open,
    which is when the coordinator keeps writing entry.data in real life.
    """
    if setup_mock is None:
        setup_mock = AsyncMock(return_value=True)
    if contracts is None:
        contracts = [_matching_contract()]
    if source == config_entries.SOURCE_REAUTH:
        form = await entry.start_reauth_flow(hass)
    else:
        form = await entry.start_reconfigure_flow(hass)
    callback = _make_callback_url(form["description_placeholders"]["authorize_url"])
    if between is not None:
        between()
    with (
        patch(
            "custom_components.haggle.config_flow._exchange_code",
            new_callable=AsyncMock,
            return_value=("acc_tok", "v1.new_token", auth_spki),
        ),
        patch(
            "custom_components.haggle.config_flow._fetch_contracts",
            new_callable=AsyncMock,
            return_value=(contracts, bff_spki),
        ),
        patch("custom_components.haggle.async_setup_entry", setup_mock),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], user_input={CALLBACK_URL_FIELD: callback}
        )
        await hass.async_block_till_done()
    return result


# --- registration -------------------------------------------------------


async def test_reconfigure_step_is_registered(hass: HomeAssistant) -> None:
    """async_step_reconfigure must exist so HA shows the Reconfigure menu item.

    `entry.supports_reconfigure` is computed as hasattr(handler, 'async_step_reconfigure')
    (config_entries.py:617-618). Without the method the button is hidden.
    """
    from custom_components.haggle.config_flow import HaggleConfigFlow

    assert hasattr(HaggleConfigFlow, "async_step_reconfigure")

    entry = _repair_entry(hass)
    form = await entry.start_reconfigure_flow(hass)
    assert entry.supports_reconfigure
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "reconfigure"
    assert "authorize_url" in form["description_placeholders"]


async def test_reconfigure_step_id_is_reconfigure_not_user(
    hass: HomeAssistant,
) -> None:
    """The Reconfigure form must show step_id='reconfigure' for its own strings."""
    entry = _repair_entry(hass)
    form = await entry.start_reconfigure_flow(hass)
    # Reconfigure has its own step_id so strings.json can carry the TOFU warning.
    assert form["step_id"] == "reconfigure"
    # Reauth still uses the 'user' step (HA adds the {name} placeholder there).
    form2 = await entry.start_reauth_flow(hass)
    assert form2["step_id"] == "user"


# --- reauth flow --------------------------------------------------------


async def test_reauth_updates_token_reason_reauth_successful(
    hass: HomeAssistant,
) -> None:
    """Reauth must write the new refresh token and abort with reauth_successful.

    Pre-fix: _async_create_entry called _abort_if_unique_id_configured(), which
    found the existing entry and aborted with 'already_configured', leaving the
    token unchanged.
    """
    entry = _repair_entry(hass)
    result = await _run_repair(hass, entry, source=config_entries.SOURCE_REAUTH)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "v1.new_token"


async def test_reauth_keeps_stored_pins_m12(hass: HomeAssistant) -> None:
    """Reauth never overwrites a stored pin (#275 M12 policy).

    Reauth is SYSTEM-initiated; an on-path attacker can provoke it.  Allowing
    reauth to re-pin would launder TLS interception silently.  Only the
    deliberately user-started Reconfigure may overwrite stored pins.
    """
    entry = _repair_entry(hass)
    # The captures (c/d) differ from the stored pins (a/b): the mismatch is
    # still true after reauth, so its warning must stay up — dismissing it
    # would let a provoked reauth silence the only signal pinning gives.
    _seed_pin_notices(hass)
    result = await _run_repair(hass, entry, source=config_entries.SOURCE_REAUTH)
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PINNED_SPKI_AUTH] == _OLD_AUTH_SPKI
    assert entry.data[CONF_PINNED_SPKI_BFF] == _OLD_BFF_SPKI
    notices = _notification_ids(hass)
    assert PIN_MISMATCH_NOTIFICATION_ID.format(host=AGL_AUTH_HOST_NAME) in notices
    assert PIN_MISMATCH_NOTIFICATION_ID.format(host=AGL_BFF_HOST_NAME) in notices


async def test_reauth_fills_empty_pins(hass: HomeAssistant) -> None:
    """Reauth fills a pin that was never captured (empty == 'no pin yet')."""
    entry = _repair_entry(
        hass,
        **{CONF_PINNED_SPKI_AUTH: "", CONF_PINNED_SPKI_BFF: ""},
    )
    result = await _run_repair(hass, entry, source=config_entries.SOURCE_REAUTH)
    assert result["reason"] == "reauth_successful"
    # Both were empty → reauth fills them.
    assert entry.data[CONF_PINNED_SPKI_AUTH] == _NEW_AUTH_SPKI
    assert entry.data[CONF_PINNED_SPKI_BFF] == _NEW_BFF_SPKI


async def test_reauth_preserves_coordinator_state(hass: HomeAssistant) -> None:
    """Coordinator-written keys (solar heal record, stall spans) must survive reauth.

    data_updates= merges into live entry.data rather than replacing it, so
    these keys are automatically preserved.
    """
    heal = {"state": "pending", "floor": "2026-01-01", "attempts": 1}
    stall = [{"start": "2026-01-01", "end": "2026-01-07"}]
    entry = _repair_entry(
        hass,
        **{CONF_SOLAR_HEAL: heal, CONF_SOLAR_STALL_SPANS: stall},
    )
    result = await _run_repair(hass, entry, source=config_entries.SOURCE_REAUTH)
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_SOLAR_HEAL] == heal
    assert entry.data[CONF_SOLAR_STALL_SPANS] == stall


async def test_reauth_contract_not_found_wrong_contract(hass: HomeAssistant) -> None:
    """Reauth with a fresh login that owns a DIFFERENT contract aborts contract_not_found.

    Nothing is written: the stored entry is unchanged.
    """
    entry = _repair_entry(hass)
    data_before = dict(entry.data)
    result = await _run_repair(
        hass,
        entry,
        source=config_entries.SOURCE_REAUTH,
        contracts=[_matching_contract(contract="1111111111")],
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "contract_not_found"
    assert dict(entry.data) == data_before
    assert not result.get("description_placeholders")


async def test_reauth_contract_not_found_no_contracts(hass: HomeAssistant) -> None:
    """Reauth with empty discovery aborts contract_not_found, entry unchanged."""
    entry = _repair_entry(hass)
    data_before = dict(entry.data)
    result = await _run_repair(
        hass, entry, source=config_entries.SOURCE_REAUTH, contracts=[]
    )
    assert result["reason"] == "contract_not_found"
    assert dict(entry.data) == data_before


# --- M3: discovered-account fail-closed ---------------------------------


async def test_m3_discovered_account_empty_fails_closed(hass: HomeAssistant) -> None:
    """M3: a discovered contract whose accountNumber is '' must NOT match.

    The account check fails CLOSED on the discovered side: a response that
    omits accountNumber cannot bypass the identity check.  An empty STORED
    account (hash/legacy entries, which never recorded one) is allowed through
    — this test exercises the DISCOVERED-empty path.
    """
    entry = _repair_entry(hass)  # stored account = "1234567890"
    data_before = dict(entry.data)
    # Contract_number matches but account_number is "" in the discovery response.
    result = await _run_repair(
        hass,
        entry,
        source=config_entries.SOURCE_REAUTH,
        contracts=[_matching_contract(account="")],
    )
    assert result["reason"] == "contract_not_found"
    assert dict(entry.data) == data_before


# --- reconfigure flow ---------------------------------------------------


async def test_reconfigure_updates_token_and_all_pins(hass: HomeAssistant) -> None:
    """Reconfigure writes the new token and re-pins both hosts.

    Pre-fix: async_step_reconfigure did not exist; the flow could not start.
    """
    entry = _repair_entry(hass)
    result = await _run_repair(hass, entry, source=config_entries.SOURCE_RECONFIGURE)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "v1.new_token"
    assert entry.data[CONF_PINNED_SPKI_AUTH] == _NEW_AUTH_SPKI
    assert entry.data[CONF_PINNED_SPKI_BFF] == _NEW_BFF_SPKI


async def test_reconfigure_preserves_coordinator_state(hass: HomeAssistant) -> None:
    """Coordinator-written solar state must survive a reconfigure."""
    heal = {"state": "done", "floor": "2026-06-01", "attempts": 3}
    entry = _repair_entry(hass, **{CONF_SOLAR_HEAL: heal})
    result = await _run_repair(hass, entry, source=config_entries.SOURCE_RECONFIGURE)
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SOLAR_HEAL] == heal


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE]
)
async def test_repair_keeps_coordinator_writes_made_while_flow_open(
    hass: HomeAssistant, source: str
) -> None:
    """Coordinator writes made WHILE the flow is open must survive its write.

    The flow stays open across the user's browser login + MFA, and the
    coordinator keeps running: it advances the heal record, appends stall
    spans and rotates the refresh token. The flow must merge into entry.data
    as it is at write time (data_updates=), never write back a snapshot taken
    when the flow started (e.g. the entry_data HA passes to
    async_step_reauth) — that would roll back the heal record and drop the
    stall spans, the only durable evidence of a give-up hole (CO-16.4).
    """
    entry = _repair_entry(
        hass,
        **{CONF_SOLAR_HEAL: {"state": "pending", "floor": "2026-09-01", "attempts": 1}},
    )
    heal_mid = {"state": "done", "floor": "2026-09-01", "attempts": 2}
    spans_mid = [{"start": "2026-09-02", "end": "2026-09-08"}]

    def _coordinator_writes() -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_SOLAR_HEAL: heal_mid,
                CONF_SOLAR_STALL_SPANS: spans_mid,
                CONF_REFRESH_TOKEN: "v1.rotated",
            },
        )

    result = await _run_repair(hass, entry, source=source, between=_coordinator_writes)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == f"{source}_successful"
    assert entry.data[CONF_SOLAR_HEAL] == heal_mid
    assert entry.data[CONF_SOLAR_STALL_SPANS] == spans_mid
    # The flow's freshly minted grant wins over the mid-flow rotation
    # (documented accepted race, threat-model I-5 — no compare-and-swap).
    assert entry.data[CONF_REFRESH_TOKEN] == "v1.new_token"


async def test_reconfigure_contract_not_found_wrong_contract(
    hass: HomeAssistant,
) -> None:
    """Reconfigure with a different contract aborts contract_not_found, nothing written."""
    entry = _repair_entry(hass)
    data_before = dict(entry.data)
    result = await _run_repair(
        hass,
        entry,
        source=config_entries.SOURCE_RECONFIGURE,
        contracts=[_matching_contract(contract="8888888888")],
    )
    assert result["reason"] == "contract_not_found"
    assert dict(entry.data) == data_before


async def test_reconfigure_gas_relabelled_contract_still_matches(
    hass: HomeAssistant,
) -> None:
    """An existing entry whose AGL contract was relabelled (e.g. gas) still matches.

    _match_existing_contract searches ALL discovered contracts (not the
    serviceable-filtered list) so a working entry can always be repaired even
    if AGL changes the fuel_type string on their side.
    """
    entry = _repair_entry(hass)
    result = await _run_repair(
        hass,
        entry,
        source=config_entries.SOURCE_REAUTH,
        contracts=[_matching_contract(fuel="gasContract")],
    )
    assert result["reason"] == "reauth_successful"


# --- entry_not_repairable -----------------------------------------------


async def test_entry_not_repairable_empty_contract_reauth(
    hass: HomeAssistant,
) -> None:
    """An entry with no stored contract number refuses reauth immediately."""
    entry = _repair_entry(hass, contract="")
    form = await entry.start_reauth_flow(hass)
    assert form["type"] is FlowResultType.ABORT
    assert form["reason"] == "entry_not_repairable"


async def test_entry_not_repairable_empty_contract_reconfigure(
    hass: HomeAssistant,
) -> None:
    """An entry with no stored contract number refuses reconfigure immediately."""
    entry = _repair_entry(hass, contract="")
    form = await entry.start_reconfigure_flow(hass)
    assert form["type"] is FlowResultType.ABORT
    assert form["reason"] == "entry_not_repairable"


# --- M1: cross-entry notification dismissal -----------------------------


async def test_m1a_two_entry_notices_kept_when_other_entry_differs(
    hass: HomeAssistant,
) -> None:
    """M1(a): pin notice stays up when another entry still holds a different pin.

    Notification ids are per HOST and shared across all Haggle entries.
    After A reconfigures with new pins, B still has the old pins — the notice
    must stay so the user knows B's certificate is still mismatched.
    B's entry.data must be byte-identical after A's reconfigure.
    """
    entry_a = _repair_entry(hass, contract="9999999999")
    entry_b = _repair_entry(hass, contract="8888888888", uid="1234567890_8888888888")
    data_b_before = dict(entry_b.data)
    _seed_pin_notices(hass)

    result = await _run_repair(hass, entry_a, source=config_entries.SOURCE_RECONFIGURE)
    assert result["reason"] == "reconfigure_successful"

    # A's pins are now new; B's are still old → notices must NOT be dismissed.
    notices = _notification_ids(hass)
    assert PIN_MISMATCH_NOTIFICATION_ID.format(host=AGL_AUTH_HOST_NAME) in notices
    assert PIN_MISMATCH_NOTIFICATION_ID.format(host=AGL_BFF_HOST_NAME) in notices
    # B's data must be entirely unmodified.
    assert dict(entry_b.data) == data_b_before


async def test_m1b_two_entry_notices_dismissed_when_all_repinned(
    hass: HomeAssistant,
) -> None:
    """M1(b): notices dismissed only when every entry matches the new pin.

    After A and B both reconfigure to the same new pins, both notices are
    dismissed — the warning is no longer true for any entry.
    """
    entry_a = _repair_entry(hass, contract="9999999999")
    entry_b = _repair_entry(hass, contract="8888888888", uid="1234567890_8888888888")
    _seed_pin_notices(hass)

    # Reconfigure A: notices still up (B has old pins).
    await _run_repair(hass, entry_a, source=config_entries.SOURCE_RECONFIGURE)
    notices_after_a = _notification_ids(hass)
    assert (
        PIN_MISMATCH_NOTIFICATION_ID.format(host=AGL_AUTH_HOST_NAME) in notices_after_a
    )

    # Reconfigure B: now both entries have the new pins → notices dismissed.
    result_b = await _run_repair(
        hass,
        entry_b,
        source=config_entries.SOURCE_RECONFIGURE,
        contracts=[_matching_contract(contract="8888888888")],
    )
    assert result_b["reason"] == "reconfigure_successful"
    notices_after_b = _notification_ids(hass)
    assert (
        PIN_MISMATCH_NOTIFICATION_ID.format(host=AGL_AUTH_HOST_NAME)
        not in notices_after_b
    )
    assert (
        PIN_MISMATCH_NOTIFICATION_ID.format(host=AGL_BFF_HOST_NAME)
        not in notices_after_b
    )


async def test_m1c_empty_pin_entry_does_not_block_dismissal(
    hass: HomeAssistant,
) -> None:
    """M1(c): an entry storing '' (legacy / pre-pinning) never blocks dismissal.

    An empty pin means "no pin yet": that entry's _check_pin is a no-op and
    never raised the shared per-host notice, so it must not keep it up.
    Otherwise a user with one legacy entry could never clear the warning by
    reconfiguring the pinned one.
    """
    entry_a = _repair_entry(hass, contract="9999999999")
    entry_b = _repair_entry(
        hass,
        contract="8888888888",
        uid="1234567890_8888888888",
        **{CONF_PINNED_SPKI_AUTH: "", CONF_PINNED_SPKI_BFF: ""},
    )
    data_b_before = dict(entry_b.data)
    _seed_pin_notices(hass)

    result = await _run_repair(hass, entry_a, source=config_entries.SOURCE_RECONFIGURE)
    assert result["reason"] == "reconfigure_successful"
    assert entry_a.data[CONF_PINNED_SPKI_AUTH] == _NEW_AUTH_SPKI
    assert entry_a.data[CONF_PINNED_SPKI_BFF] == _NEW_BFF_SPKI

    notices = _notification_ids(hass)
    assert PIN_MISMATCH_NOTIFICATION_ID.format(host=AGL_AUTH_HOST_NAME) not in notices
    assert PIN_MISMATCH_NOTIFICATION_ID.format(host=AGL_BFF_HOST_NAME) not in notices
    assert dict(entry_b.data) == data_b_before


# --- M2: partial capture ------------------------------------------------


async def test_m2_partial_capture_gives_pin_incomplete_reason(
    hass: HomeAssistant,
) -> None:
    """M2: when bff_spki capture is empty, finish with reconfigure_pin_incomplete.

    The auth pin IS updated (capture succeeded).  The bff pin is kept (capture
    failed → empty → not written).  The bff mismatch notice is left up; the
    auth notice is dismissed (because all entries now agree on the new auth pin).
    """
    entry = _repair_entry(hass)
    _seed_pin_notices(hass)

    result = await _run_repair(
        hass,
        entry,
        source=config_entries.SOURCE_RECONFIGURE,
        bff_spki="",  # BFF capture failed
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_pin_incomplete"

    # Token is updated even on a partial re-pin.
    assert entry.data[CONF_REFRESH_TOKEN] == "v1.new_token"
    # Auth pin: capture succeeded → updated.
    assert entry.data[CONF_PINNED_SPKI_AUTH] == _NEW_AUTH_SPKI
    # Bff pin: capture failed → old pin kept.
    assert entry.data[CONF_PINNED_SPKI_BFF] == _OLD_BFF_SPKI

    notices = _notification_ids(hass)
    # Auth notice: dismissed (auth was successfully re-pinned, single entry).
    assert PIN_MISMATCH_NOTIFICATION_ID.format(host=AGL_AUTH_HOST_NAME) not in notices
    # Bff notice: still up (bff was NOT re-pinned).
    assert PIN_MISMATCH_NOTIFICATION_ID.format(host=AGL_BFF_HOST_NAME) in notices


# --- M9: no sensitive data in logs --------------------------------------


async def test_m9_reauth_success_no_leak_in_logs(
    hass: HomeAssistant, caplog: Any
) -> None:
    """M9: reauth success logs must not contain any Class-B identifier or credential.

    Log line is: 'haggle reauth: contract=…NNNN pin_auth=kept pin_bff=kept'
    where only the last-4 of the contract number appears.
    """
    entry = _repair_entry(hass)
    caplog.set_level(logging.DEBUG, logger="custom_components.haggle")
    await _run_repair(hass, entry, source=config_entries.SOURCE_REAUTH)

    for secret in (
        "9999999999",
        "1234567890",
        "v1.old_token",
        "v1.new_token",
        _OLD_AUTH_SPKI,
        _NEW_AUTH_SPKI,
    ):
        assert secret not in caplog.text, f"Sensitive value leaked: {secret!r}"


async def test_m9_reconfigure_success_no_leak_in_logs(
    hass: HomeAssistant, caplog: Any
) -> None:
    """M9: reconfigure success logs must not contain any Class-B identifier or credential."""
    entry = _repair_entry(hass)
    caplog.set_level(logging.DEBUG, logger="custom_components.haggle")
    await _run_repair(hass, entry, source=config_entries.SOURCE_RECONFIGURE)

    for secret in (
        "9999999999",
        "1234567890",
        "v1.old_token",
        "v1.new_token",
        _OLD_AUTH_SPKI,
        _NEW_AUTH_SPKI,
        _OLD_BFF_SPKI,
        _NEW_BFF_SPKI,
    ):
        assert secret not in caplog.text, f"Sensitive value leaked: {secret!r}"


async def test_m9_contract_not_found_no_leak_no_placeholders(
    hass: HomeAssistant, caplog: Any
) -> None:
    """M9: contract_not_found abort must carry no description_placeholders.

    No contract or account should appear in the abort result or logs — the
    error text is generic ('contract not found') to avoid confirming which
    account the user holds.
    """
    entry = _repair_entry(hass)
    caplog.set_level(logging.DEBUG, logger="custom_components.haggle")
    result = await _run_repair(
        hass,
        entry,
        source=config_entries.SOURCE_REAUTH,
        contracts=[_matching_contract(contract="1111111111")],
    )
    assert result["reason"] == "contract_not_found"
    assert not result.get("description_placeholders")

    for secret in ("9999999999", "1234567890", "v1.old_token", "v1.new_token"):
        assert secret not in caplog.text, f"Sensitive value leaked: {secret!r}"


# --- verification follow-ups (#275 M7 mutation table) -------------------


@pytest.mark.parametrize(
    ("exc", "error"),
    [(AGLAuthError("x"), "invalid_auth"), (AGLError("x"), "cannot_connect")],
)
async def test_reconfigure_exchange_error_keeps_reconfigure_step(
    hass: HomeAssistant, exc: Exception, error: str
) -> None:
    """M7(d): an exchange error re-shows the RECONFIGURE form, not 'user'.

    HA routes the next submit to async_step_<step_id>; re-showing 'user'
    would drop the re-pin warning and the retry would run as a plain login.
    A second submit (same state) with a working exchange then completes.
    """
    entry = _repair_entry(hass)
    form = await entry.start_reconfigure_flow(hass)
    callback = _make_callback_url(form["description_placeholders"]["authorize_url"])
    with patch(
        "custom_components.haggle.config_flow._exchange_code",
        new_callable=AsyncMock,
        side_effect=exc,
    ):
        retry = await hass.config_entries.flow.async_configure(
            form["flow_id"], user_input={CALLBACK_URL_FIELD: callback}
        )
    assert retry["type"] is FlowResultType.FORM
    assert retry["step_id"] == "reconfigure"
    assert retry["errors"] == {"base": error}

    with (
        patch(
            "custom_components.haggle.config_flow._exchange_code",
            new_callable=AsyncMock,
            return_value=("acc_tok", "v1.new_token", _NEW_AUTH_SPKI),
        ),
        patch(
            "custom_components.haggle.config_flow._fetch_contracts",
            new_callable=AsyncMock,
            return_value=([_matching_contract()], _NEW_BFF_SPKI),
        ),
        patch(
            "custom_components.haggle.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            retry["flow_id"],
            user_input={
                CALLBACK_URL_FIELD: _make_callback_url(
                    retry["description_placeholders"]["authorize_url"]
                )
            },
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PINNED_SPKI_AUTH] == _NEW_AUTH_SPKI


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE]
)
async def test_repair_reloads_the_entry(hass: HomeAssistant, source: str) -> None:
    """M7(h): the repaired entry is RELOADED so the new grant/pins take effect.

    async_update_and_abort (no reload) would leave the running instance on
    the dead token and the old in-memory pin-check closure.
    """
    entry = _repair_entry(hass)
    if source == config_entries.SOURCE_REAUTH:
        form = await entry.start_reauth_flow(hass)
    else:
        form = await entry.start_reconfigure_flow(hass)
    setup = AsyncMock(return_value=True)
    with (
        patch(
            "custom_components.haggle.config_flow._exchange_code",
            new_callable=AsyncMock,
            return_value=("acc_tok", "v1.new_token", _NEW_AUTH_SPKI),
        ),
        patch(
            "custom_components.haggle.config_flow._fetch_contracts",
            new_callable=AsyncMock,
            return_value=([_matching_contract()], _NEW_BFF_SPKI),
        ),
        patch("custom_components.haggle.async_setup_entry", setup),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            user_input={
                CALLBACK_URL_FIELD: _make_callback_url(
                    form["description_placeholders"]["authorize_url"]
                )
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    setup.assert_awaited_once()


async def test_reconfigure_cancels_pending_reauth_flow(hass: HomeAssistant) -> None:
    """M7(h): Reconfigure also clears a pending reauth prompt (via the reload).

    Backs the user guidance that Reconfigure fixes a dead grant AND a pin
    mismatch in one go.
    """
    entry = _repair_entry(hass)
    reauth = await entry.start_reauth_flow(hass)
    assert reauth["type"] is FlowResultType.FORM

    def _pending_reauth() -> list[Any]:
        return hass.config_entries.flow.async_progress_by_handler(
            DOMAIN,
            match_context={
                "entry_id": entry.entry_id,
                "source": config_entries.SOURCE_REAUTH,
            },
        )

    assert _pending_reauth()
    result = await _run_repair(hass, entry, source=config_entries.SOURCE_RECONFIGURE)
    assert result["reason"] == "reconfigure_successful"
    assert not _pending_reauth()


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE]
)
@pytest.mark.parametrize(
    "contracts",
    [
        pytest.param([_matching_contract(account="5555555555")], id="moved_account"),
        pytest.param([_gas("1111111111")], id="gas_other"),
    ],
)
async def test_existing_entry_contract_absent_aborts_without_writing(
    hass: HomeAssistant, source: str, contracts: list[Contract]
) -> None:
    """M7(f): the entry's contract under a DIFFERENT account is not a match.

    Nothing is written, nothing reloads and the seeded notices stay up.
    """
    entry = _repair_entry(hass)
    data_before = dict(entry.data)
    _seed_pin_notices(hass)
    setup = AsyncMock(return_value=True)
    result = await _run_repair(
        hass, entry, source=source, contracts=contracts, setup_mock=setup
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "contract_not_found"
    assert dict(entry.data) == data_before
    setup.assert_not_awaited()
    notices = _notification_ids(hass)
    for host in (AGL_AUTH_HOST_NAME, AGL_BFF_HOST_NAME):
        assert PIN_MISMATCH_NOTIFICATION_ID.format(host=host) in notices


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE]
)
async def test_existing_entry_flow_never_shows_contract_picker(
    hass: HomeAssistant, source: str
) -> None:
    """Two serviceable contracts: a repair flow matches its own, no picker."""
    entry = _repair_entry(hass)
    result = await _run_repair(
        hass,
        entry,
        source=source,
        contracts=[_matching_contract(contract="1111111111"), _matching_contract()],
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"].endswith("_successful")
    assert entry.data[CONF_CONTRACT_NUMBER] == "9999999999"


async def test_hash_unique_id_entry_matches_by_contract_number(
    hass: HomeAssistant,
) -> None:
    """A hash-unique_id entry with no stored account repairs by contract only.

    Its unique_id (sha256 of the ORIGINAL token) can never be recomputed, so
    the match must key on entry.data; nothing is back-filled.
    """
    entry = _repair_entry(hass, account="", uid="0123456789abcdef")
    result = await _run_repair(hass, entry, source=config_entries.SOURCE_REAUTH)
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == "0123456789abcdef"
    assert entry.data[CONF_ACCOUNT_NUMBER] == ""
    assert entry.data[CONF_REFRESH_TOKEN] == "v1.new_token"


async def test_reauth_contract_fetch_failure_then_retry_succeeds(
    hass: HomeAssistant,
) -> None:
    """A discovery error mid-reauth is retryable and still updates the entry."""
    entry = _repair_entry(hass)
    form = await entry.start_reauth_flow(hass)
    with (
        patch(
            "custom_components.haggle.config_flow._exchange_code",
            new_callable=AsyncMock,
            return_value=("acc_tok", "v1.new_token", _NEW_AUTH_SPKI),
        ),
        patch(
            "custom_components.haggle.config_flow._fetch_contracts",
            new_callable=AsyncMock,
            side_effect=[AGLError("x"), ([_matching_contract()], _NEW_BFF_SPKI)],
        ),
        patch(
            "custom_components.haggle.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        failed = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            user_input={
                CALLBACK_URL_FIELD: _make_callback_url(
                    form["description_placeholders"]["authorize_url"]
                )
            },
        )
        assert failed["type"] is FlowResultType.FORM
        assert failed["step_id"] == "select_contract"
        assert failed["errors"] == {"base": "cannot_connect"}
        result = await hass.config_entries.flow.async_configure(failed["flow_id"], {})
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "v1.new_token"


_A, _B, _C = "a" * 64, "b" * 64, "c" * 64


@pytest.mark.parametrize(
    ("source", "stored", "capture", "expected_updates", "expected_repinned"),
    [
        # Reconfigure: every non-empty capture is written and counted.
        (config_entries.SOURCE_RECONFIGURE, _A, _C, {"k": _C}, True),
        (config_entries.SOURCE_RECONFIGURE, _A, _A, {"k": _A}, True),
        (config_entries.SOURCE_RECONFIGURE, "", _C, {"k": _C}, True),
        (config_entries.SOURCE_RECONFIGURE, _A, "", {}, False),
        (config_entries.SOURCE_RECONFIGURE, "", "", {}, False),
        # Reauth: fills a missing pin only; never overwrites; never re-pins.
        (config_entries.SOURCE_REAUTH, _A, _C, {}, False),
        (config_entries.SOURCE_REAUTH, _A, _A, {}, False),
        (config_entries.SOURCE_REAUTH, "", _C, {"k": _C}, False),
        (config_entries.SOURCE_REAUTH, _A, "", {}, False),
        (config_entries.SOURCE_REAUTH, "", "", {}, False),
    ],
)
def test_pin_updates_policy(
    source: str,
    stored: str,
    capture: str,
    expected_updates: dict[str, str],
    expected_repinned: bool,
) -> None:
    """The pure pin policy (M12): the same row applied to BOTH hosts."""
    from custom_components.haggle.config_flow import _pin_updates

    stored_data = {CONF_PINNED_SPKI_AUTH: stored, CONF_PINNED_SPKI_BFF: stored}
    updates, repinned = _pin_updates(source, stored_data, capture, capture)
    want = {
        key: value
        for key in (CONF_PINNED_SPKI_AUTH, CONF_PINNED_SPKI_BFF)
        for value in expected_updates.values()
    }
    assert updates == want
    assert repinned == (
        [AGL_AUTH_HOST_NAME, AGL_BFF_HOST_NAME] if expected_repinned else []
    )


def test_flow_strings_mirror_and_cover_new_reasons() -> None:
    """strings.json == translations/en.json, and every new #275 key exists."""
    from pathlib import Path

    base = Path(__file__).parent.parent / "custom_components" / "haggle"
    strings = json.loads((base / "strings.json").read_text())
    english = json.loads((base / "translations" / "en.json").read_text())
    assert strings == english
    config = strings["config"]
    assert "{authorize_url}" in config["step"]["reconfigure"]["description"]
    for reason in (
        "reauth_successful",
        "reconfigure_successful",
        "reconfigure_pin_incomplete",
        "contract_not_found",
        "entry_not_repairable",
    ):
        assert config["abort"][reason]
