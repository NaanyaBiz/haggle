"""Tests for AglAuth and AglClient."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
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
    # Real JSON text, not the Python repr — the non-200 branch re-parses the
    # body to classify the error slug (PR #265).
    mock_resp.text = AsyncMock(return_value=json.dumps(response_data))
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
            pytest.raises(AGLError) as exc_info,
        ):
            await auth.async_force_refresh(session)

        # A 429 says nothing about the grant — retryable, never reauth
        # (Codex taxonomy finding, PR #265).
        assert not isinstance(exc_info.value, AGLAuthError)
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


class TestMalformedButOkTokenResponses:
    """#243 — a 200 with a valid-JSON but wrong-shaped body must not escape.

    Every coordinator catch site is built around the AGLError family. A raw
    AttributeError/KeyError/ValueError from the schema-trusting block bypasses
    all of it, crashes the update cycle before the #155 retry cadence runs, and
    lands in DataUpdateCoordinator.last_exception — which diagnostics.py
    publishes verbatim into a file users attach to public GitHub issues.

    None of these are auth failures, so none may raise AGLAuthError: that
    would trigger a reauth prompt and burn a working grant over a bad response.
    """

    @staticmethod
    async def _refresh(body: object) -> None:
        async def persist(token: str) -> None:
            pass

        await AglAuth("v1.initial", persist).async_force_refresh(_make_session(body))  # type: ignore[arg-type]

    @pytest.mark.parametrize("body", [None, [], "a string", 3, True])
    async def test_non_object_body_raises_retryable_agl_error(
        self, body: object
    ) -> None:
        with pytest.raises(AGLError) as exc:
            await self._refresh(body)
        assert not isinstance(exc.value, AGLAuthError)

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"access_token": "a"},
            {"refresh_token": "r"},
            {"access_token": "a", "refresh_token": None},
            {"access_token": ["a"], "refresh_token": "r"},
        ],
    )
    async def test_missing_or_mistyped_tokens_raise_agl_error(self, body: dict) -> None:
        with pytest.raises(AGLError) as exc:
            await self._refresh(body)
        assert not isinstance(exc.value, AGLAuthError)

    async def test_malformed_expires_in_preserves_the_rotated_grant(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Codex P1 (PR #265): valid tokens + bad expires_in must NOT raise.

        Auth0 has already rotated the refresh token by the time the body is
        parsed. The first cut raised AGLTransportError from the same try
        block that extracted the tokens, BEFORE _persist() ran — so the
        "retryable" retry submitted the stale token, got invalid_grant, and
        forced the reauth lockout the shield exists to prevent. A malformed
        expires_in now degrades to the documented 900 s default, and the
        hostile value never reaches the log message (int()'s ValueError
        embeds its input).
        """
        hostile = "<script>alert(1)</script>"
        persisted: list[str] = []

        async def persist(token: str) -> None:
            persisted.append(token)

        auth = AglAuth("v1.initial", persist)
        session = _make_session(
            {"access_token": "a", "refresh_token": "v1.rotated", "expires_in": hostile}
        )
        with caplog.at_level("WARNING"):
            token = await auth.async_force_refresh(session)

        assert token == "a"
        assert persisted == ["v1.rotated"]  # the grant survived
        assert auth._refresh_token == "v1.rotated"
        assert not any(hostile in r.message for r in caplog.records)

    async def test_absurd_expires_in_defaults_instead_of_raising(self) -> None:
        """10**20 overflowed fromtimestamp; now clamped to the 900 s default."""
        persisted: list[str] = []

        async def persist(token: str) -> None:
            persisted.append(token)

        auth = AglAuth("v1.initial", persist)
        session = _make_session(
            {"access_token": "a", "refresh_token": "v1.rotated", "expires_in": 10**20}
        )
        token = await auth.async_force_refresh(session)

        assert token == "a"
        assert persisted == ["v1.rotated"]

    async def test_blank_token_strings_are_rejected_not_persisted(self) -> None:
        """Codex (PR #265): "" passes isinstance(str) but must never persist.

        Persisting a blank refresh token permanently discards the real grant.
        """
        persisted: list[str] = []

        async def persist(token: str) -> None:
            persisted.append(token)

        auth = AglAuth("v1.initial", persist)
        # Whitespace-only is as unusable as empty — truthy, so it slipped
        # past the pass-2 non-empty check (Codex pass 3).
        for body in (
            {"access_token": "", "refresh_token": ""},
            {"access_token": "a", "refresh_token": " "},
            {"access_token": "\t", "refresh_token": "r"},
            # Control chars survive a .strip() check ("r\n".strip() == "r"
            # is nonempty) but persist an invalid credential and break
            # Authorization-header construction (Codex pass 4).
            {"access_token": "a", "refresh_token": "r\n"},
            {"access_token": "a\r", "refresh_token": "r"},
        ):
            session = _make_session(body)
            with pytest.raises(AGLError) as exc:
                await auth.async_force_refresh(session)
            assert not isinstance(exc.value, AGLAuthError)

        assert persisted == []
        assert auth._refresh_token == "v1.initial"

    async def test_structured_error_field_is_not_echoed(self) -> None:
        """`error` reaches HA notifications — only a plausible slug is echoed.

        A structured/malformed error also says nothing about the grant, so it
        must stay retryable (Codex, PR #265).
        """
        hostile = "x" * 500
        with pytest.raises(AGLError) as exc:
            await self._refresh({"error": {"nested": hostile}})
        assert not isinstance(exc.value, AGLAuthError)
        assert hostile not in str(exc.value)
        assert "unspecified" in str(exc.value)

    async def test_short_nonslug_error_is_not_echoed(self) -> None:
        """Codex (PR #265): short ≠ safe — enforce the OAuth slug alphabet.

        A token fragment, email address, or control-character payload all fit
        in 64 chars; none may reach notifications/diagnostics verbatim.
        """
        for hostile in ("user@example.com", "eyJhbGciOi.frag", "a\x1b[2Jb"):
            with pytest.raises(AGLError) as exc:
                await self._refresh({"error": hostile})
            assert hostile not in str(exc.value)
            assert "unspecified" in str(exc.value)

    async def test_ordinary_error_slug_still_surfaces(self) -> None:
        """The useful case is preserved: a known terminal slug still reauths."""
        with pytest.raises(AGLAuthError) as exc:
            await self._refresh({"error": "invalid_grant"})
        assert "invalid_grant" in str(exc.value)

    async def test_unknown_error_slug_is_retryable_but_echoed(self) -> None:
        """An unrecognised (but well-formed) slug is reported, not reauthed.

        `{"error": "upstream_failure"}` says nothing about the refresh grant;
        only _TERMINAL_GRANT_ERRORS may burn it (Codex, PR #265).
        """
        with pytest.raises(AGLError) as exc:
            await self._refresh({"error": "upstream_failure"})
        assert not isinstance(exc.value, AGLAuthError)
        assert "upstream_failure" in str(exc.value)

    async def test_http403_invalid_grant_still_reauths(self) -> None:
        """Auth0 delivers a dead grant as HTTP 403 + JSON body — must reauth."""
        session = _make_session({"error": "invalid_grant"}, status=403)

        async def persist(token: str) -> None:
            pass

        auth = AglAuth("v1.initial", persist)
        with pytest.raises(AGLAuthError):
            await auth.async_force_refresh(session)

    async def test_deeply_nested_json_body_stays_in_the_aglerror_family(
        self,
    ) -> None:
        """json.loads raises RecursionError (not ValueError) on ~100k nesting.

        A raw RecursionError bypasses every AGLError catch site and crashes
        the update cycle before the retry machinery runs (#151 class;
        Codex pass 5). Both the 200 path and the non-200 slug parse must
        wrap it.
        """
        nested = "[" * 100_000 + "]" * 100_000

        async def persist(token: str) -> None:
            pass

        # 200 path: resp.json itself raising RecursionError.
        session = _make_session({}, status=200)
        resp = session.post.return_value
        resp.json = AsyncMock(side_effect=RecursionError("depth"))
        auth = AglAuth("v1.initial", persist)
        with pytest.raises(AGLError) as exc:
            await auth.async_force_refresh(session)
        assert not isinstance(exc.value, AGLAuthError)

        # non-200 path: the slug re-parse of hostile text.
        session = _make_session({}, status=503)
        session.post.return_value.text = AsyncMock(return_value=nested)
        auth = AglAuth("v1.initial", persist)
        with pytest.raises(AGLError) as exc:
            await auth.async_force_refresh(session)
        assert not isinstance(exc.value, AGLAuthError)

    async def test_http500_is_retryable_not_reauth(self) -> None:
        """An Auth0 5xx is a blip, not a dead grant (Codex taxonomy, PR #265)."""
        session = _make_session({"error": "server_error"}, status=500)

        async def persist(token: str) -> None:
            pass

        auth = AglAuth("v1.initial", persist)
        with pytest.raises(AGLError) as exc:
            await auth.async_force_refresh(session)
        assert not isinstance(exc.value, AGLAuthError)

    async def test_non_string_id_token_is_dropped_not_stored(self) -> None:
        """A mistyped id_token degrades to "" rather than poisoning TokenSet."""

        async def persist(token: str) -> None:
            pass

        auth = AglAuth("v1.initial", persist)
        await auth.async_force_refresh(
            _make_session(
                {"access_token": "a", "refresh_token": "r", "id_token": {"bad": 1}}
            )
        )
        assert auth._token_set is not None
        assert auth._token_set.id_token == ""


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
        # kWh comes from outer consumption.quantity (the real meter read),
        # NOT consumption.values.quantity (a DPI/chart-scaled helper that
        # undercounts by 4-73% — confirmed against AGL portal CSV 2026-05-12).
        kwhs = {r.kwh for r in readings}
        assert 0.175 in kwhs
        assert 0.186 in kwhs
        # The inner values.quantity must NOT leak through as kWh.
        assert 0.112 not in kwhs
        assert 0.119 not in kwhs
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

    async def test_non_json_200_body_raises_agl_error(self) -> None:
        """A 200 with a non-JSON body (e.g. an Akamai challenge page) must
        become AGLError, not a raw JSONDecodeError that crashes the update
        cycle and bypasses the solar heal's attempt accounting (#151)."""
        client, session = self._make_client({}, status=200)
        session.get.return_value.json = AsyncMock(
            side_effect=json.JSONDecodeError("Expecting value", "<html>", 0)
        )
        with (
            patch.object(
                client._auth,
                "async_ensure_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            pytest.raises(AGLError, match="non-JSON"),
        ):
            await client.async_get_overview()

    async def test_transport_error_raises_agl_error(self) -> None:
        """aiohttp transport failures wrap into AGLError (#151)."""
        client, session = self._make_client({}, status=200)
        session.get = MagicMock(side_effect=aiohttp.ClientError("conn reset"))
        with (
            patch.object(
                client._auth,
                "async_ensure_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            pytest.raises(AGLError, match="transport error"),
        ):
            await client.async_get_overview()

    async def test_timeout_raises_agl_error(self) -> None:
        """TimeoutError (alias of asyncio.TimeoutError) wraps into AGLError
        (#151)."""
        client, session = self._make_client({}, status=200)
        session.get = MagicMock(side_effect=TimeoutError())
        with (
            patch.object(
                client._auth,
                "async_ensure_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            pytest.raises(AGLError, match="transport error"),
        ):
            await client.async_get_overview()

    async def test_force_refresh_transport_error_is_not_auth_error(self) -> None:
        """A network blip during token refresh must be retryable AGLError,
        never AGLAuthError (which triggers the reauth flow) and never a raw
        escape (#151)."""
        session = MagicMock()
        session.post = MagicMock(side_effect=aiohttp.ClientError("dns fail"))
        auth = AglAuth("v1.tok", AsyncMock())
        with pytest.raises(AGLError, match="transport error") as exc_info:
            await auth.async_force_refresh(session)
        assert not isinstance(exc_info.value, AGLAuthError)

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
# #32: stale, never-called AglClient methods are gone
# ---------------------------------------------------------------------------


class TestIntervalWindowIsWiredThrough:
    """#242 — the parser's window guard is inert unless the client passes the day.

    The vulnerability was precisely that the client built `period=` and then
    discarded it at the parser boundary, so these assert the wiring rather than
    the guard (which tests/test_parser.py covers).
    """

    @staticmethod
    async def _captured(method: str) -> dict:
        from datetime import date

        session = _make_session({})
        auth = AglAuth("v1.tok", AsyncMock())
        with patch(
            "custom_components.haggle.agl.client.AglAuth.async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="tok",
        ):
            client = AglClient(auth, session)
            with patch(
                "custom_components.haggle.agl.client.parse_interval_readings",
                return_value=[],
            ) as parser:
                await getattr(client, method)("9999999999", date(2026, 7, 1))
        return parser.call_args.kwargs

    async def test_current_hourly_passes_expected_day(self) -> None:
        from datetime import date

        kwargs = await self._captured("async_get_usage_hourly")
        assert kwargs["expected_day"] == date(2026, 7, 1)

    async def test_previous_hourly_passes_expected_day(self) -> None:
        from datetime import date

        kwargs = await self._captured("async_get_usage_hourly_previous")
        assert kwargs["expected_day"] == date(2026, 7, 1)

    async def test_solar_passes_expected_day_and_keeps_source_field(self) -> None:
        from datetime import date

        kwargs = await self._captured("async_get_solar_hourly")
        assert kwargs["expected_day"] == date(2026, 7, 1)
        assert kwargs["source_field"] == "feedIn"


def test_unused_methods_removed_from_client() -> None:
    """Belt-and-braces: re-introducing these without a caller is a regression."""
    assert not hasattr(AglClient, "async_get_servicehub")
    assert not hasattr(AglClient, "async_get_usage_daily")
    assert not hasattr(AglClient, "async_close")


_SOLAR_HOURLY_RESPONSE = {
    "resourceType": "electricity-solar",
    "granularity": "hourly",
    "timeZone": "Australia/Sydney",
    "sections": [
        {
            "startDate": "2026-06-29",
            "items": [
                {
                    "dateTime": "2026-06-29T02:00:00Z",
                    "consumption": {
                        "values": {"amount": 0.07, "quantity": 0.07},
                        "amount": 0.041,
                        "quantity": 0.112,
                        "type": "normal",
                    },
                    "feedIn": {
                        "values": {"amount": 0.79, "quantity": 0.79},
                        "amount": 0.0421,
                        "quantity": 1.276,
                        "type": "normal",
                    },
                },
                {
                    "dateTime": "2026-06-29T13:00:00Z",
                    "consumption": {
                        "values": {"amount": 0.05, "quantity": 0.05},
                        "amount": 0.025,
                        "quantity": 0.067,
                        "type": "normal",
                    },
                    "feedIn": {
                        "values": {"amount": 0.0, "quantity": 0.0},
                        "amount": 0.0,
                        "quantity": 0.0,
                        "type": "normal",
                    },
                },
            ],
        }
    ],
}


class TestAglClientSolar:
    def _make_client(
        self, response_data: dict, status: int = 200
    ) -> tuple[AglClient, MagicMock]:
        session = _make_session(response_data, status)
        auth = AglAuth("v1.tok", AsyncMock())
        client = AglClient(auth, session)
        return client, session

    async def test_get_solar_hourly_url_and_feedin_parsing(self) -> None:
        from datetime import date

        client, session = self._make_client(_SOLAR_HOURLY_RESPONSE)
        with patch.object(
            client._auth,
            "async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="tok",
        ):
            readings = await client.async_get_solar_hourly(
                "9999999999", date(2026, 6, 29)
            )

        url = session.get.call_args[0][0]
        assert "/api/v2/usage/smart/ElectricitySolar/9999999999/Current/Hourly" in url
        assert "period=2026-06-29_2026-06-29" in url
        assert "scaling=" in url

        # feedIn outer quantity/amount only; the zero-on-zero night slot drops.
        assert len(readings) == 1
        assert readings[0].kwh == 1.276
        assert readings[0].cost_aud == 0.0421
        # The consumption block must not leak into the feedIn series.
        assert readings[0].kwh != 0.112

    async def test_get_solar_hourly_previous_variant(self) -> None:
        from datetime import date

        client, session = self._make_client(_SOLAR_HOURLY_RESPONSE)
        with patch.object(
            client._auth,
            "async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="tok",
        ):
            await client.async_get_solar_hourly(
                "9999999999", date(2026, 6, 29), previous=True
            )

        url = session.get.call_args[0][0]
        assert "/ElectricitySolar/9999999999/Previous/Hourly" in url
