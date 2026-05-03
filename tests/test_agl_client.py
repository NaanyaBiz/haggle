"""Tests for AglAuth and AglClient."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.haggle.agl.client import (
    AglAuth,
    AGLAuthError,
    AglClient,
    AGLError,
    AGLRateLimitError,
)
from custom_components.haggle.agl.models import Contract, IntervalReading, PlanRates

# ---------------------------------------------------------------------------
# Synthetic response fixtures (same shape as live AGL API responses)
# ---------------------------------------------------------------------------

_OVERVIEW_RESPONSE = {
    "accounts": [
        {
            "contracts": [
                {
                    "hasSolar": False,
                    "contractNumber": "9999999999",
                    "type": "electricityContract",
                    "status": "active",
                    "meterType": "smart",
                    "additionalLabelValue": "$90.00",
                }
            ],
            "address": "1 Sample Street SUBURB QLD 4000",
            "type": "energyAccount",
            "accountNumber": "1234567890",
        }
    ]
}

_HOURLY_RESPONSE = {
    "resourceType": "electricity",
    "granularity": "hourly",
    "timeZone": "Australia/Sydney",
    "sections": [
        {
            "startDate": "2024-01-15",
            "items": [
                {
                    "dateTime": "2024-01-15T13:30:00Z",
                    "consumption": {
                        "values": {"amount": 0.112, "quantity": 0.112},
                        "amount": 0.059,
                        "quantity": 0.175,
                        "type": "normal",
                    },
                },
                {
                    "dateTime": "2024-01-15T13:00:00Z",
                    "consumption": {
                        "values": {"amount": 0.119, "quantity": 0.119},
                        "amount": 0.063,
                        "quantity": 0.186,
                        "type": "normal",
                    },
                },
                {
                    # type=none should be filtered out
                    "dateTime": "2024-01-15T14:00:00Z",
                    "consumption": {
                        "values": {"amount": 0.0, "quantity": 0.0},
                        "amount": 0.0,
                        "quantity": 0.0,
                        "type": "none",
                    },
                },
            ],
        }
    ],
}

_PLAN_RESPONSE = {
    "contractNumber": "9999999999",
    "productName": "Smart Saver",
    "gstInclusiveRates": [
        {"kind": "header", "title": "T11 General Usage**"},
        {
            "kind": "detail",
            "title": "First 379 kWh",
            "type": "c/kWh",
            "price": 33.792,
            "validTo": "9999-12-31",
        },
        {
            "kind": "detail",
            "title": "Thereafter",
            "type": "c/kWh",
            "price": 33.792,
            "validTo": "9999-12-31",
        },
        {
            "kind": "detail",
            "title": "Supply charge",
            "type": "c/day",
            "price": 131.714,
            "validTo": "9999-12-31",
        },
    ],
}

_TOKEN_RESPONSE = {
    "access_token": "eyFAKE.eyFAKE.sig",
    "refresh_token": "v1.rotated_token_456",
    "id_token": "eyFAKE.id.sig",
    "expires_in": 900,
    "token_type": "Bearer",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(response_data: dict, status: int = 200) -> MagicMock:
    """Return a mock aiohttp.ClientSession that returns response_data as JSON."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=response_data)
    mock_resp.text = AsyncMock(return_value=str(response_data))
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=mock_resp)
    session.get = MagicMock(return_value=mock_resp)
    session.request = MagicMock(return_value=mock_resp)
    return session


# ---------------------------------------------------------------------------
# AglAuth tests
# ---------------------------------------------------------------------------


class TestAglAuth:
    async def test_force_refresh_returns_access_token(self) -> None:
        persisted: list[str] = []

        async def persist(token: str) -> None:
            persisted.append(token)

        session = _make_session(_TOKEN_RESPONSE)
        auth = AglAuth("v1.initial", persist)
        token = await auth.async_force_refresh(session)

        assert token == "eyFAKE.eyFAKE.sig"
        assert persisted == ["v1.rotated_token_456"]
        assert auth._refresh_token == "v1.rotated_token_456"

    async def test_ensure_valid_token_uses_cached_when_fresh(self) -> None:
        """If token is fresh (mocked exp far in future), skip refresh."""
        persisted: list[str] = []

        async def persist(token: str) -> None:
            persisted.append(token)

        auth = AglAuth("v1.initial", persist)

        # Inject a fake TokenSet with a JWT whose exp is far in the future.
        # We can't easily make a real JWT, so we patch _decode_jwt_exp instead.
        from custom_components.haggle.agl.models import TokenSet

        future_exp = int(datetime.now(tz=UTC).timestamp()) + 3600
        auth._token_set = TokenSet(
            access_token="cached_token",
            refresh_token="v1.existing",
            expires_at=datetime.fromtimestamp(future_exp + 900, tz=UTC),
        )

        with patch(
            "custom_components.haggle.agl.client._decode_jwt_exp",
            return_value=future_exp,
        ):
            session = MagicMock()
            token = await auth.async_ensure_valid_token(session)

        assert token == "cached_token"
        assert persisted == []  # no refresh happened

    async def test_force_refresh_raises_auth_error_on_401(self) -> None:
        session = _make_session({}, status=401)

        async def persist(token: str) -> None:
            pass

        auth = AglAuth("v1.initial", persist)
        with pytest.raises(AGLAuthError):
            await auth.async_force_refresh(session)

    async def test_force_refresh_redacts_body_from_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """SAST-003: Auth0 error body must stay in DEBUG logs, not in the raised exception.

        ConfigEntryAuthFailed(str(err)) reaches HA Persistent Notifications;
        Auth0 error bodies may include diagnostic fields that should not surface
        there.
        """
        # Synthetic body: must not look enough like a real JWT to trip secret
        # scanners, but still contain a marker we can grep for in assertions.
        sensitive_body = {
            "error": "rate_limited",
            "error_description": "MARKER-SHOULD-NOT-LEAK",
        }
        session = _make_session(sensitive_body, status=429)

        async def persist(token: str) -> None:
            pass

        auth = AglAuth("v1.initial", persist)
        with (
            caplog.at_level("DEBUG", logger="custom_components.haggle.agl.client"),
            pytest.raises(AGLAuthError) as exc_info,
        ):
            await auth.async_force_refresh(session)

        # The exception message must mention the status code but NOT the body.
        assert "429" in str(exc_info.value)
        assert "MARKER-SHOULD-NOT-LEAK" not in str(exc_info.value)
        assert "rate_limited" not in str(exc_info.value)
        # Body was logged at DEBUG.
        assert any("MARKER-SHOULD-NOT-LEAK" in r.message for r in caplog.records)

    async def test_force_refresh_raises_on_error_field(self) -> None:
        session = _make_session(
            {"error": "invalid_grant", "error_description": "Refresh token expired"},
            status=200,
        )

        async def persist(token: str) -> None:
            pass

        auth = AglAuth("v1.initial", persist)
        with pytest.raises(AGLAuthError, match="invalid_grant"):
            await auth.async_force_refresh(session)


# ---------------------------------------------------------------------------
# AglClient tests
# ---------------------------------------------------------------------------


class TestAglClient:
    def _make_client(
        self, response_data: dict, status: int = 200
    ) -> tuple[AglClient, MagicMock]:
        session = _make_session(response_data, status)
        auth = AglAuth("v1.tok", AsyncMock())
        auth._token_set = MagicMock()
        auth._token_set.access_token = "test_access_token"

        with patch(
            "custom_components.haggle.agl.client.AglAuth.async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="test_access_token",
        ):
            client = AglClient(auth, session)
        return client, session

    async def test_get_overview_parses_contracts(self) -> None:
        client, _ = self._make_client(_OVERVIEW_RESPONSE)
        with patch.object(
            client._auth,
            "async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="tok",
        ):
            contracts = await client.async_get_overview()

        assert len(contracts) == 1
        c = contracts[0]
        assert isinstance(c, Contract)
        assert c.contract_number == "9999999999"
        assert c.account_number == "1234567890"
        assert c.fuel_type == "electricityContract"
        assert c.has_solar is False

    async def test_get_usage_hourly_parses_intervals(self) -> None:
        from datetime import date

        client, _ = self._make_client(_HOURLY_RESPONSE)
        with patch.object(
            client._auth,
            "async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="tok",
        ):
            readings = await client.async_get_usage_hourly(
                "9999999999", date(2024, 1, 15)
            )

        # type=none slot should be filtered out → 2 readings
        assert len(readings) == 2
        assert all(isinstance(r, IntervalReading) for r in readings)
        # Values are from consumption.values.quantity (not consumption.quantity)
        kwhs = {r.kwh for r in readings}
        assert 0.112 in kwhs
        assert 0.119 in kwhs
        # Confirm dateTime is UTC
        assert all(r.dt.tzinfo == UTC for r in readings)

    async def test_get_plan_parses_rates(self) -> None:
        client, _ = self._make_client(_PLAN_RESPONSE)
        with patch.object(
            client._auth,
            "async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="tok",
        ):
            plan = await client.async_get_plan("9999999999")

        assert isinstance(plan, PlanRates)
        assert plan.product_name == "Smart Saver"
        assert plan.supply_charge_cents_per_day == pytest.approx(131.714)
        assert any(r.get("type") == "c/kWh" for r in plan.unit_rates)

    async def test_rate_limit_raises(self) -> None:
        client, _ = self._make_client({}, status=429)
        with (
            patch.object(
                client._auth,
                "async_ensure_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            pytest.raises(AGLRateLimitError),
        ):
            await client.async_get_overview()

    async def test_http_error_raises_agl_error(self) -> None:
        client, _ = self._make_client({}, status=500)
        with (
            patch.object(
                client._auth,
                "async_ensure_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            pytest.raises(AGLError),
        ):
            await client.async_get_overview()

    async def test_http_error_keeps_url_and_body_out_of_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """SAST-004: contract_number-bearing URL + response body stay in DEBUG."""
        sensitive_body = {"detail": "internal MARKER2-SHOULD-NOT-LEAK"}
        client, _ = self._make_client(sensitive_body, status=500)

        with (
            caplog.at_level("DEBUG", logger="custom_components.haggle.agl.client"),
            patch.object(
                client._auth,
                "async_ensure_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            pytest.raises(AGLError) as exc_info,
        ):
            # contract_number is part of the URL path
            await client.async_get_usage_summary("9999999999_PII")

        msg = str(exc_info.value)
        assert "500" in msg
        assert "9999999999_PII" not in msg  # URL not in exception
        assert "MARKER2-SHOULD-NOT-LEAK" not in msg  # body not in exception
        # But both are present in DEBUG.
        assert any("9999999999_PII" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Pin-check wiring (PR 4 — TOFU SPKI pinning)
# ---------------------------------------------------------------------------


class TestPinCheckWiring:
    """AglAuth and AglClient invoke the pin_check callback after successful TLS."""

    async def test_force_refresh_invokes_pin_check_with_auth_host(self) -> None:
        from custom_components.haggle.agl.pinning import AGL_AUTH_HOST_NAME

        observed: list[tuple[str, str]] = []

        def pin_check(host: str, spki: str) -> None:
            observed.append((host, spki))

        session = _make_session(_TOKEN_RESPONSE)
        with patch(
            "custom_components.haggle.agl.client.get_peer_spki_hash",
            return_value="abc123" * 10 + "deadbe",
        ):
            auth = AglAuth("v1.initial", AsyncMock(), pin_check=pin_check)
            await auth.async_force_refresh(session)

        assert observed == [(AGL_AUTH_HOST_NAME, "abc123" * 10 + "deadbe")]

    async def test_get_invokes_pin_check_with_bff_host(self) -> None:
        from custom_components.haggle.agl.pinning import AGL_BFF_HOST_NAME

        observed: list[tuple[str, str]] = []

        def pin_check(host: str, spki: str) -> None:
            observed.append((host, spki))

        session = _make_session(_OVERVIEW_RESPONSE)
        auth = AglAuth("v1.tok", AsyncMock())
        client = AglClient(auth, session, pin_check=pin_check)

        with (
            patch.object(
                client._auth,
                "async_ensure_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "custom_components.haggle.agl.client.get_peer_spki_hash",
                return_value="bffspki" * 9 + "x",
            ),
        ):
            await client.async_get_overview()

        assert observed == [(AGL_BFF_HOST_NAME, "bffspki" * 9 + "x")]

    async def test_pin_check_callback_exception_does_not_break_request(self) -> None:
        """A throwing pin_check must not poison the data path."""

        def bad_pin(host: str, spki: str) -> None:
            raise RuntimeError("validator on fire")

        session = _make_session(_OVERVIEW_RESPONSE)
        auth = AglAuth("v1.tok", AsyncMock())
        client = AglClient(auth, session, pin_check=bad_pin)

        with (
            patch.object(
                client._auth,
                "async_ensure_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "custom_components.haggle.agl.client.get_peer_spki_hash",
                return_value="x" * 64,
            ),
        ):
            contracts = await client.async_get_overview()

        assert len(contracts) == 1  # request still succeeded

    async def test_no_pin_check_when_callback_is_none(self) -> None:
        """Default (no callback) path runs cleanly even when SPKI returns None."""
        session = _make_session(_OVERVIEW_RESPONSE)
        auth = AglAuth("v1.tok", AsyncMock())
        client = AglClient(auth, session)  # pin_check=None default

        with patch.object(
            client._auth,
            "async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="tok",
        ):
            contracts = await client.async_get_overview()

        assert len(contracts) == 1
