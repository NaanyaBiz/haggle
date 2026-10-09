"""Tests for custom_components/haggle/agl/parser.py."""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.haggle.agl.models import (
    BillPeriod,
    Contract,
    DailyReading,
    IntervalReading,
    PlanRates,
)
from custom_components.haggle.agl.parser import (
    _default_api_tz,
    _zone_for_key,
    parse_bill_period,
    parse_daily_readings,
    parse_interval_readings,
    parse_overview,
    parse_plan,
    relocalise_agl_timestamp,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Numeric guard (SAST-008)
# ---------------------------------------------------------------------------


class TestSafeFloat:
    """Adversarial / corrupt API values must clamp to 0.0 instead of poisoning stats."""

    def test_finite_positive_passes_through(self) -> None:
        from custom_components.haggle.agl.parser import safe_float

        assert safe_float(0.5) == 0.5
        assert safe_float("12.34") == pytest.approx(12.34)

    def test_inf_nan_negative_clamp_to_zero(self) -> None:
        from custom_components.haggle.agl.parser import safe_float

        assert safe_float(float("inf")) == 0.0
        assert safe_float(float("nan")) == 0.0
        assert safe_float(-1.0) == 0.0
        assert safe_float(1e400) == 0.0  # overflow → inf → clamped

    def test_large_but_finite_is_rejected(self) -> None:
        """#241 — "finite" was never a sufficient bound.

        This assertion previously read
        `assert safe_float("1e308") == 1e308  # finite, allowed`, encoding the
        gap as intended behaviour. It is not: 1e308 is finite, so it passed the
        isfinite() check unchanged, and `1e308 + 1e308` evaluates to `inf` with
        no exception raised. Two such readings in one hourly bucket — or a
        cumulative sum crossing the ceiling — silently produced the very
        non-finite `sum` the finite check exists to prevent.
        """
        from custom_components.haggle.agl.parser import safe_float

        assert float("inf") == 1e308 + 1e308  # the mechanism, made explicit
        assert safe_float("1e308") == 0.0
        assert safe_float(1e308) == 0.0

    def test_bound_admits_plausible_values_and_rejects_just_above(self) -> None:
        """The bound sits far above any real reading, so it never clips data."""
        from custom_components.haggle.agl.parser import safe_float
        from custom_components.haggle.const import MAX_AGL_NUMERIC

        assert safe_float(50.0) == 50.0  # a big 30-min household slot
        assert safe_float(9_999.0) == 9_999.0  # a quarterly bill total
        assert safe_float(MAX_AGL_NUMERIC) == MAX_AGL_NUMERIC  # inclusive
        assert safe_float(MAX_AGL_NUMERIC * 1.000001) == 0.0

    def test_rejected_value_becomes_zero_not_the_bound(self) -> None:
        """Rejects to 0.0, never clamps to the ceiling.

        A zero delta leaves the cumulative sum untouched; writing MAX_AGL_NUMERIC
        instead would burn a permanent, enormous false spike into the series.
        """
        from custom_components.haggle.agl.parser import safe_float
        from custom_components.haggle.const import MAX_AGL_NUMERIC

        assert safe_float(1e300) != MAX_AGL_NUMERIC
        assert safe_float(1e300) == 0.0

    def test_negative_zero_normalises(self) -> None:
        """coordinator.py's removed copy returned -0.0 here; this one does not.

        Mechanism note: -0.0 is falsy, so it normalises via the `raw or 0.0`
        short-circuit BEFORE the `< 0` guard is ever reached — not via the
        negative-value rejection path (review finding). Pinned here so a
        refactor dropping the `or 0.0` shorthand re-fails this test.
        """
        from custom_components.haggle.agl.parser import safe_float

        assert repr(safe_float(-0.0)) == "0.0"
        # The guard path proper, for contrast:
        assert safe_float(-0.5) == 0.0

    def test_unparseable_clamps_to_zero(self) -> None:
        from custom_components.haggle.agl.parser import safe_float

        assert safe_float(None) == 0.0
        assert safe_float("not a number") == 0.0
        assert safe_float({}) == 0.0


# ---------------------------------------------------------------------------
# parse_plan allowlist (SAST-007)
# ---------------------------------------------------------------------------


class TestParsePlanAllowlist:
    """Open-schema dict(rate) is gone — only four documented keys propagate."""

    def test_only_known_keys_land_in_unit_rates(self) -> None:
        data = {
            "productName": "Smart Saver",
            "gstInclusiveRates": [
                {
                    "kind": "detail",
                    "type": "c/kWh",
                    "title": "Peak",
                    "price": 33.792,
                    "validTo": "9999-12-31",
                    # Attacker-injected keys must NOT propagate.
                    "evil_callback": "https://attacker.example/x",
                    "__proto__": "polluted",
                }
            ],
        }
        plan = parse_plan(data)
        assert len(plan.unit_rates) == 1
        rate = plan.unit_rates[0]
        assert set(rate.keys()) == {"kind", "type", "title", "price"}
        assert "evil_callback" not in rate
        assert "validTo" not in rate

    def test_many_overbound_prices_emit_one_summary_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Codex pass 6 (PR #266): per-row WARNINGs over an unbounded rates
        list are the same MITM log-flood vector fixed for intervals."""

        data = {
            "productName": "Hostile",
            "gstInclusiveRates": [
                {"kind": "detail", "type": "c/kWh", "title": f"r{i}", "price": 1e300}
                for i in range(500)
            ],
        }
        with caplog.at_level(logging.WARNING):
            plan = parse_plan(data)
        assert all(r["price"] == 0.0 for r in plan.unit_rates)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "500" in warnings[0].getMessage()

    def test_extreme_price_clamped_to_zero(self) -> None:
        data = {
            "productName": "Smart Saver",
            "gstInclusiveRates": [
                {
                    "kind": "detail",
                    "type": "c/kWh",
                    "title": "Peak",
                    "price": float("inf"),
                }
            ],
        }
        plan = parse_plan(data)
        assert plan.unit_rates[0]["price"] == 0.0


# ---------------------------------------------------------------------------
# parse_interval_readings
# ---------------------------------------------------------------------------


class TestParseIntervalReadings:
    def test_filters_none_type(self) -> None:
        """Items with type='none' must be dropped."""
        data = load_fixture("hourly_response.json")
        readings = parse_interval_readings(data)
        assert all(r.rate_type != "none" for r in readings)

    def test_uses_outer_consumption_quantity_not_inner_values(self) -> None:
        """kWh must come from consumption.quantity (outer), NOT values.quantity (inner).

        Reconciled 2026-05-12 against an AGL portal "MyUsageData" CSV across
        11 mitm /Hourly captures: outer ``consumption.quantity`` matches the
        portal-grade meter value to 0.001 kWh, while ``consumption.values.quantity``
        is a DPI/chart-scaled helper and undercounts by 4-73%.

        The fixture has values.quantity=0.112 but outer quantity=0.175 for the
        first item — we must get 0.175.
        """
        data = load_fixture("hourly_response.json")
        readings = parse_interval_readings(data)
        kwhs = {r.kwh for r in readings}
        # Outer consumption.quantity values from the fixture (the real meter reads).
        assert 0.175 in kwhs
        assert 0.186 in kwhs
        # The inner values.quantity (chart helper) must NOT appear as kWh.
        assert 0.112 not in kwhs
        assert 0.119 not in kwhs

    def test_uses_outer_consumption_amount_for_cost(self) -> None:
        """Cost AUD must come from consumption.amount (outer), not values.amount."""
        data = load_fixture("hourly_response.json")
        readings = parse_interval_readings(data)
        costs = {r.cost_aud for r in readings}
        # Outer consumption.amount values from the fixture.
        assert 0.059 in costs
        assert 0.063 in costs
        # The peak slot has outer amount 0.489 and inner amount 0.925 —
        # we must see 0.489 (the real cost).
        assert 0.489 in costs
        assert 0.925 not in costs

    def test_filters_zero_on_zero_placeholders(self) -> None:
        """Slots with kwh=0 AND cost=0 are AGL placeholders (data not ready).

        AGL returns these for days where the AEMO meter reads have not yet
        been delivered — even with a non-``none`` type. Inserting them as
        zero-state rows would create phantom flat days in the statistics
        table that the resume logic would skip past forever once AGL had the
        real reads.
        """
        data = load_fixture("hourly_response.json")
        readings = parse_interval_readings(data)
        # Fixture has one type=normal slot at 14:30 UTC with all-zero values
        # — it must be filtered out.
        for r in readings:
            assert not (r.kwh == 0.0 and r.cost_aud == 0.0)

    def test_dt_is_tz_aware_utc(self) -> None:
        """Every parsed datetime must be UTC-aware."""
        data = load_fixture("hourly_response.json")
        readings = parse_interval_readings(data)
        assert len(readings) > 0
        for r in readings:
            assert r.dt.tzinfo is not None
            assert r.dt.tzinfo == UTC

    def test_expected_count_after_filters(self) -> None:
        """Fixture has 8 items; 1 has type=none, 1 is zero-on-zero → 6 readings."""
        data = load_fixture("hourly_response.json")
        readings = parse_interval_readings(data)
        assert len(readings) == 6

    def test_returns_interval_reading_instances(self) -> None:
        data = load_fixture("hourly_response.json")
        readings = parse_interval_readings(data)
        assert all(isinstance(r, IntervalReading) for r in readings)

    def test_peak_type_preserved(self) -> None:
        """The peak-type slot must not be dropped and rate_type must be 'peak'."""
        data = load_fixture("hourly_response.json")
        readings = parse_interval_readings(data)
        peak_readings = [r for r in readings if r.rate_type == "peak"]
        assert len(peak_readings) == 1
        # Peak slot outer quantity is 1.448, outer amount is 0.489.
        assert peak_readings[0].kwh == pytest.approx(1.448)
        assert peak_readings[0].cost_aud == pytest.approx(0.489)

    def test_empty_sections_returns_empty_list(self) -> None:
        readings = parse_interval_readings({"sections": []})
        assert readings == []

    def test_invalid_datetime_is_skipped(self) -> None:
        """Items with unparseable dateTime are silently skipped."""
        data = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "not-a-date",
                            "consumption": {
                                "quantity": 0.5,
                                "amount": 0.1,
                                "type": "normal",
                            },
                        }
                    ]
                }
            ]
        }
        readings = parse_interval_readings(data)
        assert readings == []

    def test_filters_pending_type(self) -> None:
        """Intervals with type='pending' (AEMO data not yet available) must be dropped."""
        data = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "2026-07-01T00:00:00Z",
                            "consumption": {
                                "type": "pending",
                                "quantity": 0.5,
                                "amount": 0.1,
                            },
                        },
                        {
                            "dateTime": "2026-07-01T00:30:00Z",
                            "consumption": {
                                "type": "normal",
                                "quantity": 0.3,
                                "amount": 0.05,
                            },
                        },
                    ]
                }
            ]
        }
        readings = parse_interval_readings(data)
        assert len(readings) == 1
        assert readings[0].rate_type == "normal"


# ---------------------------------------------------------------------------
# parse_overview
# ---------------------------------------------------------------------------


class TestIntervalWindowValidation:
    """#242 — returned timestamps must be checked against the requested day.

    Not merely a "row on the wrong day" concern: coordinator._import_intervals
    derives its cumulative-sum baseline cutoff as min(hour_cons), straight from
    response content. One interval with an old timestamp pins that cutoff
    before all real recorder history, so the baseline resolves to 0.0 instead
    of the true multi-year total and the same import writes today's genuine
    hours on top of it — a large downward step in the sum column (#114 class,
    but triggerable by a single crafted timestamp).
    """

    @staticmethod
    def _payload(dt_iso: str) -> dict:
        return {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": dt_iso,
                            "consumption": {
                                "type": "normal",
                                "quantity": 1.5,
                                "amount": 0.45,
                            },
                        }
                    ]
                }
            ]
        }

    def test_far_past_timestamp_is_dropped(self) -> None:
        """The attack: a 1970-era slot would pin the baseline cutoff at zero."""
        readings = parse_interval_readings(
            self._payload("1970-01-02T00:00:00Z"), expected_day=date(2026, 7, 1)
        )
        assert readings == []

    def test_far_future_timestamp_is_dropped(self) -> None:
        readings = parse_interval_readings(
            self._payload("2099-01-01T00:00:00Z"), expected_day=date(2026, 7, 1)
        )
        assert readings == []

    def test_requested_day_is_kept(self) -> None:
        readings = parse_interval_readings(
            self._payload("2026-07-01T03:00:00Z"), expected_day=date(2026, 7, 1)
        )
        assert len(readings) == 1

    @pytest.mark.parametrize(
        "dt_iso",
        [
            "2026-06-30T14:00:00Z",  # AEST local midnight of the 1st
            "2026-07-01T23:30:00Z",  # UTC-12: mid local day
            "2026-07-02T11:30:00Z",  # UTC-12 tail (last slot of the local day)
            "2026-06-30T10:00:00Z",  # UTC+14 head (local midnight of the 1st)
        ],
    )
    def test_adjacent_utc_dates_are_kept(self, dt_iso: str) -> None:
        """AGL reads period= in LOCAL time and returns UTC, so a one-day query
        legitimately spans two UTC dates. The window must not clip those."""
        readings = parse_interval_readings(
            self._payload(dt_iso), expected_day=date(2026, 7, 1)
        )
        assert len(readings) == 1

    def test_window_is_opt_in(self) -> None:
        """Without expected_day the parser is unchanged (fuzz harness path)."""
        readings = parse_interval_readings(self._payload("1970-01-02T00:00:00Z"))
        assert len(readings) == 1

    def test_only_out_of_window_items_are_dropped(self) -> None:
        """A poisoned item is removed without discarding the legitimate ones."""
        payload = self._payload("2026-07-01T03:00:00Z")
        payload["sections"][0]["items"].append(
            {
                "dateTime": "1970-01-02T00:00:00Z",
                "consumption": {"type": "normal", "quantity": 2.0, "amount": 0.6},
            }
        )
        readings = parse_interval_readings(payload, expected_day=date(2026, 7, 1))

        assert len(readings) == 1
        assert readings[0].dt.date() == date(2026, 7, 1)
        # The cutoff coordinator._import_intervals would derive is now safe.
        assert min(r.dt for r in readings).year == 2026

    def test_solar_path_also_validates(self) -> None:
        payload = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "1970-01-02T00:00:00Z",
                            "feedIn": {
                                "type": "normal",
                                "quantity": 3.0,
                                "amount": 0.5,
                            },
                        }
                    ]
                }
            ]
        }
        readings = parse_interval_readings(
            payload, source_field="feedIn", expected_day=date(2026, 7, 1)
        )
        assert readings == []


class TestIntervalWindowTzDerived:
    """Codex P1 (PR #266): the ±1-DATE window alone is too loose.

    For an AEST contract, day D's true UTC shape is [D-1T14:00Z, D T14:00Z).
    The date window accepted EVERY instant of D-1, so an injected D-1T00:00Z
    reading survived, became min(hour_cons), and pulled the baseline cutoff
    ~14 h early — stored rows in that gap were excluded from the baseline but
    not re-emitted, so the first genuine row stepped the cumulative sum down
    (#114 class). With tz the window is the local day plus a TRAILING-only
    2 h slack — pass 2 showed leading slack re-admits the attack at its own
    width, while a late row cannot lower min(hour_cons).
    """

    _payload = staticmethod(TestIntervalWindowValidation._payload)
    _BRISBANE = ZoneInfo("Australia/Brisbane")  # +10, no DST
    _SYDNEY = ZoneInfo("Australia/Sydney")  # +10/+11, DST

    def test_adjacent_date_injection_is_dropped(self) -> None:
        """The exact Codex attack: D-1T00:00Z passes the date window, not tz."""
        readings = parse_interval_readings(
            self._payload("2026-06-30T00:00:00Z"),
            expected_day=date(2026, 7, 1),
            tz=self._BRISBANE,
        )
        assert readings == []

    @pytest.mark.parametrize(
        "dt_iso",
        [
            "2026-06-30T14:00:00Z",  # local midnight — first slot of the day
            "2026-07-01T13:30:00Z",  # 23:30 local — last slot of the day
            "2026-07-01T15:59:00Z",  # inside the 2 h TRAILING slack (kept)
        ],
    )
    def test_legitimate_boundary_slots_are_kept(self, dt_iso: str) -> None:
        readings = parse_interval_readings(
            self._payload(dt_iso),
            expected_day=date(2026, 7, 1),
            tz=self._BRISBANE,
        )
        assert len(readings) == 1

    @pytest.mark.parametrize(
        "dt_iso",
        [
            # Codex pass-2 P1: leading slack of ANY width re-admits the
            # baseline-cutoff attack at that width — the lower bound is the
            # local midnight itself, so even one second before it is out.
            "2026-06-30T13:59:59Z",
            "2026-06-30T12:00:00Z",  # the old leading-slack edge — now out
            "2026-07-01T16:00:00Z",  # at the trailing-slack edge (exclusive)
        ],
    )
    def test_outside_the_asymmetric_window_is_dropped(self, dt_iso: str) -> None:
        readings = parse_interval_readings(
            self._payload(dt_iso),
            expected_day=date(2026, 7, 1),
            tz=self._BRISBANE,
        )
        assert readings == []

    @pytest.mark.parametrize(
        "dt_iso",
        [
            "2026-10-03T14:00:00Z",  # local midnight (AEST, +10)
            "2026-10-04T12:30:00Z",  # 23:30 local (AEDT, +11) — 23 h day
        ],
    )
    def test_dst_transition_day_boundaries_are_kept(self, dt_iso: str) -> None:
        """2026-10-04 is Sydney's 23 h DST-start day; tzinfo handles the
        asymmetric midnights that a fixed-offset window would clip."""
        readings = parse_interval_readings(
            self._payload(dt_iso),
            expected_day=date(2026, 10, 4),
            tz=self._SYDNEY,
        )
        assert len(readings) == 1

    def test_injection_cannot_move_the_baseline_cutoff(self) -> None:
        """min(r.dt) — the coordinator's baseline cutoff — stays the genuine
        local-midnight slot even with the adjacent-date poison present."""
        payload = self._payload("2026-06-30T14:00:00Z")
        payload["sections"][0]["items"].append(
            {
                "dateTime": "2026-06-30T00:00:00Z",  # the poison
                "consumption": {"type": "normal", "quantity": 2.0, "amount": 0.6},
            }
        )
        readings = parse_interval_readings(
            payload, expected_day=date(2026, 7, 1), tz=self._BRISBANE
        )
        assert len(readings) == 1
        assert min(r.dt for r in readings) == datetime(2026, 6, 30, 14, tzinfo=UTC)

    def test_solar_path_uses_tz_window_too(self) -> None:
        payload = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "2026-06-30T00:00:00Z",
                            "feedIn": {
                                "type": "normal",
                                "quantity": 3.0,
                                "amount": 0.5,
                            },
                        }
                    ]
                }
            ]
        }
        readings = parse_interval_readings(
            payload,
            source_field="feedIn",
            expected_day=date(2026, 7, 1),
            tz=self._BRISBANE,
        )
        assert readings == []


class TestTzForAddress:
    """Contract-local timezone from the service address (Codex pass 3, #266).

    The window must be the CONTRACT's local day; the state token before the
    postcode is the best locality signal the API exposes. None (→ caller's
    HA-tz fallback) whenever it doesn't parse.
    """

    @pytest.mark.parametrize(
        ("address", "key"),
        [
            ("1 Sample Street SUBURB QLD 4000", "Australia/Brisbane"),
            ("2 Example Rd TOWN NSW 2000", "Australia/Sydney"),
            ("3 Test Ave PLACE ACT 2600", "Australia/Sydney"),
            ("4 Demo St SPOT VIC 3000", "Australia/Melbourne"),
            ("5 Trial Ct AREA SA 5000", "Australia/Adelaide"),
            ("6 Mock Ln ZONE WA 6000", "Australia/Perth"),
            # Sub-state exception: Broken Hill runs +9:30/+10:30, 30 min
            # behind Sydney — the state zone would re-open a 30-minute
            # leading window on the baseline cutoff (Codex pass 5).
            ("7 Mine Rd BROKEN HILL NSW 2880", "Australia/Broken_Hill"),
        ],
    )
    def test_state_maps_to_timezone(self, address: str, key: str) -> None:
        from custom_components.haggle.agl.parser import tz_for_address

        tz = tz_for_address(address)
        assert tz is not None
        assert getattr(tz, "key", None) == key

    @pytest.mark.parametrize(
        "address",
        [
            "",
            "1 Sample Street SUBURB 4000",  # no state token
            "WA Street WOODVILLE",  # state token without a postcode after it
            "totally unstructured",
        ],
    )
    def test_unparseable_address_returns_none(self, address: str) -> None:
        from custom_components.haggle.agl.parser import tz_for_address

        assert tz_for_address(address) is None


class TestParseOverview:
    def test_extracts_contracts(self) -> None:
        data = load_fixture("overview_response.json")
        contracts = parse_overview(data)
        assert len(contracts) == 1

    def test_contract_fields(self) -> None:
        data = load_fixture("overview_response.json")
        contracts = parse_overview(data)
        c = contracts[0]
        assert isinstance(c, Contract)
        assert c.contract_number == "9999999999"
        assert c.account_number == "1234567890"
        assert c.address == "1 Sample Street SUBURB QLD 4000"
        assert c.fuel_type == "electricityContract"
        assert c.status == "active"
        assert c.has_solar is False
        assert c.meter_type == "smart"

    def test_bill_projection_read_from_overview(self) -> None:
        """The projection comes from /v3/overview, its only source (#253).

        The usage-summary endpoint does not carry `additionalLabelValue` —
        confirmed by the sensor reading `unknown` in every release through
        v0.4.0 — so parse_bill_period alone can never populate it.
        """
        contracts = parse_overview(load_fixture("overview_response.json"))
        assert contracts[0].bill_projection_label == "$90.00"

    def test_projection_ignored_when_label_is_not_a_projection(self) -> None:
        """additionalLabelValue is only read when its label says "projection".

        AGL reuses the same slot for different quantities, so a positional
        read would publish the wrong number under the projection sensor.
        """
        data = load_fixture("overview_response.json")
        contract = data["accounts"][0]["contracts"][0]
        contract["additionalLabel"] = "Sold To Grid"
        contract["additionalLabelValue"] = "+ $7.43"

        assert parse_overview(data)[0].bill_projection_label == ""

    def test_projection_label_match_is_case_insensitive(self) -> None:
        """Casing/wording drift on AGL's side degrades to the value, not silence."""
        data = load_fixture("overview_response.json")
        data["accounts"][0]["contracts"][0]["additionalLabel"] = "ESTIMATED PROJECTION"

        assert parse_overview(data)[0].bill_projection_label == "$90.00"

    def test_projection_absent_when_label_pair_missing(self) -> None:
        """A contract with no additional-label pair yields no projection."""
        data = load_fixture("overview_response.json")
        contract = data["accounts"][0]["contracts"][0]
        del contract["additionalLabel"]
        del contract["additionalLabelValue"]

        assert parse_overview(data)[0].bill_projection_label == ""

    def test_empty_accounts_returns_empty(self) -> None:
        contracts = parse_overview({"accounts": []})
        assert contracts == []

    def test_multiple_contracts_in_one_account(self) -> None:
        data = {
            "accounts": [
                {
                    "accountNumber": "ACC1",
                    "address": "1 Test St",
                    "contracts": [
                        {
                            "contractNumber": "C1",
                            "type": "electricityContract",
                            "status": "active",
                            "meterType": "smart",
                            "hasSolar": False,
                        },
                        {
                            "contractNumber": "C2",
                            "type": "gasContract",
                            "status": "active",
                            "meterType": "basic",
                            "hasSolar": False,
                        },
                    ],
                }
            ]
        }
        contracts = parse_overview(data)
        assert len(contracts) == 2
        assert {c.contract_number for c in contracts} == {"C1", "C2"}
        assert all(c.account_number == "ACC1" for c in contracts)


# ---------------------------------------------------------------------------
# parse_bill_period
# ---------------------------------------------------------------------------


class TestParseBillPeriod:
    def test_returns_bill_period_instance(self) -> None:
        data = load_fixture("bill_period_response.json")
        bp = parse_bill_period(data)
        assert isinstance(bp, BillPeriod)

    def test_correct_start_and_end_dates(self) -> None:
        data = load_fixture("bill_period_response.json")
        bp = parse_bill_period(data)
        assert bp.start == date(2024, 1, 1)
        assert bp.end == date(2024, 1, 31)

    def test_cost_label(self) -> None:
        data = load_fixture("bill_period_response.json")
        bp = parse_bill_period(data)
        assert bp.cost_label == "$45.00"

    def test_projection_label_from_root(self) -> None:
        """projection_label comes from root additionalLabelValue."""
        data = load_fixture("bill_period_response.json")
        bp = parse_bill_period(data)
        assert bp.projection_label == "$90.00"

    def test_consumption_kwh_parsed_from_quantity_string(self) -> None:
        data = load_fixture("bill_period_response.json")
        bp = parse_bill_period(data)
        assert bp.consumption_kwh == pytest.approx(200.0)

    def test_missing_bill_period_returns_today_dates(self) -> None:
        """Empty response should not crash; dates fall back to today."""
        from datetime import UTC, datetime as _dt

        bp = parse_bill_period({})
        assert bp.start == _dt.now(UTC).date()
        assert bp.end == _dt.now(UTC).date()


# ---------------------------------------------------------------------------
# parse_plan
# ---------------------------------------------------------------------------


class TestParsePlan:
    def test_returns_plan_rates_instance(self) -> None:
        data = load_fixture("plan_response.json")
        plan = parse_plan(data)
        assert isinstance(plan, PlanRates)

    def test_product_name(self) -> None:
        data = load_fixture("plan_response.json")
        plan = parse_plan(data)
        assert plan.product_name == "Smart Saver"

    def test_supply_charge(self) -> None:
        data = load_fixture("plan_response.json")
        plan = parse_plan(data)
        assert plan.supply_charge_cents_per_day == pytest.approx(131.714)

    def test_unit_rates_contain_c_kwh_entries(self) -> None:
        data = load_fixture("plan_response.json")
        plan = parse_plan(data)
        kwh_rates = [r for r in plan.unit_rates if r.get("type") == "c/kWh"]
        assert len(kwh_rates) == 2
        for r in kwh_rates:
            assert r["price"] == pytest.approx(33.792)

    def test_header_entries_excluded_from_unit_rates(self) -> None:
        """kind='header' rows must not appear in unit_rates."""
        data = load_fixture("plan_response.json")
        plan = parse_plan(data)
        assert all(r.get("kind") != "header" for r in plan.unit_rates)

    def test_empty_rates_list(self) -> None:
        plan = parse_plan({"productName": "Test", "gstInclusiveRates": []})
        assert plan.product_name == "Test"
        assert plan.unit_rates == []
        assert plan.supply_charge_cents_per_day == 0.0


# ---------------------------------------------------------------------------
# parse_daily_readings
# ---------------------------------------------------------------------------


class TestParseDailyReadings:
    def test_parse_daily_filters_none(self) -> None:
        data = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "2026-04-28T00:00:00Z",
                            "consumption": {
                                "quantity": 5.2,
                                "amount": 1.75,
                                "type": "normal",
                            },
                        },
                        {
                            "dateTime": "2026-04-29T00:00:00Z",
                            "consumption": {
                                "quantity": 0.0,
                                "amount": 0.0,
                                "type": "none",
                            },
                        },
                    ]
                }
            ]
        }
        readings = parse_daily_readings(data)
        assert len(readings) == 1
        assert isinstance(readings[0], DailyReading)

    def test_daily_reading_date_field(self) -> None:
        data = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "2026-04-28T00:00:00Z",
                            "consumption": {
                                "quantity": 5.2,
                                "amount": 1.75,
                                "type": "normal",
                            },
                        }
                    ]
                }
            ]
        }
        readings = parse_daily_readings(data)
        assert readings[0].day == date(2026, 4, 28)

    def test_daily_uses_outer_consumption_quantity(self) -> None:
        """Daily kWh must come from outer consumption.quantity (matches AEMO CSV).

        Inner ``values.quantity`` is a DPI/chart helper and must not be read.
        """
        data = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "2026-04-28T00:00:00Z",
                            "consumption": {
                                "values": {"amount": 5.2, "quantity": 5.2},
                                "amount": 9.78,
                                "quantity": 29.044,
                                "type": "normal",
                            },
                        }
                    ]
                }
            ]
        }
        readings = parse_daily_readings(data)
        assert readings[0].kwh == pytest.approx(29.044)
        assert readings[0].cost_aud == pytest.approx(9.78)

    def test_daily_filters_zero_on_zero_placeholder(self) -> None:
        """Daily slots with kwh=0 AND cost=0 are AGL placeholders → filtered."""
        data = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "2026-04-28T00:00:00Z",
                            "consumption": {
                                "quantity": 0.0,
                                "amount": 0.0,
                                "type": "normal",
                            },
                        }
                    ]
                }
            ]
        }
        readings = parse_daily_readings(data)
        assert readings == []

    def test_daily_filters_pending_type(self) -> None:
        """Daily slots with type='pending' must be dropped."""
        data = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "2026-07-01T00:00:00Z",
                            "consumption": {
                                "type": "pending",
                                "quantity": 5.2,
                                "amount": 1.75,
                            },
                        },
                        {
                            "dateTime": "2026-07-02T00:00:00Z",
                            "consumption": {
                                "type": "normal",
                                "quantity": 4.8,
                                "amount": 1.60,
                            },
                        },
                    ]
                }
            ]
        }
        readings = parse_daily_readings(data)
        assert len(readings) == 1
        assert readings[0].day == date(2026, 7, 2)


# ---------------------------------------------------------------------------
# Time-of-Use: tariff classification + plan rate mapping
# ---------------------------------------------------------------------------


class TestClassifyTariff:
    """The free-text → ToU band heuristic (parser._classify_tariff)."""

    def test_shoulder_matches(self) -> None:
        from custom_components.haggle.agl.parser import _classify_tariff

        assert _classify_tariff("Shoulder") == "shoulder"
        assert _classify_tariff("T93 Shoulder usage") == "shoulder"

    def test_offpeak_variants_match_before_peak(self) -> None:
        from custom_components.haggle.agl.parser import _classify_tariff

        # "off peak" contains the substring "peak"; off-peak must win.
        assert _classify_tariff("Off Peak") == "offpeak"
        assert _classify_tariff("off-peak") == "offpeak"
        assert _classify_tariff("OFFPEAK") == "offpeak"

    def test_peak_matches(self) -> None:
        from custom_components.haggle.agl.parser import _classify_tariff

        assert _classify_tariff("Peak") == "peak"

    def test_general_usage_is_unmatched(self) -> None:
        from custom_components.haggle.agl.parser import _classify_tariff

        assert _classify_tariff("First 379 kWh") is None
        assert _classify_tariff("Thereafter") is None
        assert _classify_tariff("T11 General Usage**") is None


class TestParsePlanTou:
    def test_tou_plan_maps_all_three_bands(self) -> None:
        plan = parse_plan(load_fixture("tou_plan_response.json"))
        assert plan.tou_unit_rates == {
            "peak": pytest.approx(41.9),
            "shoulder": pytest.approx(22.55),
            "offpeak": pytest.approx(18.04),
        }
        # Supply charge + flat unit_rates list still populated.
        assert plan.supply_charge_cents_per_day == pytest.approx(131.714)
        assert any(r["type"] == "c/kWh" for r in plan.unit_rates)

    def test_flat_plan_has_no_tou_rates(self) -> None:
        plan = parse_plan(load_fixture("plan_response.json"))
        assert plan.tou_unit_rates == {}

    def test_first_rate_per_band_wins(self) -> None:
        """Tiered c/kWh rows under one header collapse to the first price."""
        data = {
            "gstInclusiveRates": [
                {"kind": "header", "title": "Peak"},
                {
                    "kind": "detail",
                    "type": "c/kWh",
                    "price": 40.0,
                    "title": "First 100",
                },
                {
                    "kind": "detail",
                    "type": "c/kWh",
                    "price": 50.0,
                    "title": "Thereafter",
                },
            ]
        }
        plan = parse_plan(data)
        assert plan.tou_unit_rates == {"peak": pytest.approx(40.0)}


class TestParsePlanFeedInRate:
    """Solar feed-in tariff lives in gstExclusiveRates (#128, FiT is GST-free)."""

    def test_solar_plan_feed_in_rate_parsed(self) -> None:
        plan = parse_plan(load_fixture("solar_plan_response.json"))
        assert plan.feed_in_rate_cents_per_kwh == pytest.approx(1.2)
        # The gstInclusiveRates side is unaffected.
        assert plan.supply_charge_cents_per_day == pytest.approx(140.0)

    def test_flat_plan_without_feed_in_is_none(self) -> None:
        plan = parse_plan(load_fixture("plan_response.json"))
        assert plan.feed_in_rate_cents_per_kwh is None

    def test_unrelated_gst_exclusive_rows_ignored(self) -> None:
        """Only a c/kWh detail row titled feed-in can set the rate."""
        data = {
            "gstExclusiveRates": [
                {
                    "kind": "detail",
                    "type": "c/day",
                    "price": 90.0,
                    "title": "Membership",
                },
                {"kind": "header", "title": "Solar feed-in"},
                {"kind": "detail", "type": "c/kWh", "price": 5.0, "title": "Demand"},
            ]
        }
        plan = parse_plan(data)
        assert plan.feed_in_rate_cents_per_kwh is None

    def test_feed_in_title_variants_match(self) -> None:
        for title in ("Solar feed-in", "Solar Feed In tariff", "FEED-IN credit"):
            data = {
                "gstExclusiveRates": [
                    {"kind": "detail", "type": "c/kWh", "price": 3.3, "title": title}
                ]
            }
            assert parse_plan(data).feed_in_rate_cents_per_kwh == pytest.approx(3.3)


class TestParseIntervalReadingsTou:
    def test_mixed_tou_intervals_preserve_rate_type(self) -> None:
        readings = parse_interval_readings(load_fixture("tou_hourly_response.json"))
        # type=none is filtered; the four tariff types survive.
        types = {r.rate_type for r in readings}
        assert types == {"peak", "shoulder", "offpeak", "normal"}
        # kWh from outer consumption.quantity, not inner values.quantity.
        peak = [r for r in readings if r.rate_type == "peak"]
        assert sorted(r.kwh for r in peak) == [pytest.approx(0.5), pytest.approx(1.0)]


class TestParseSolarIntervals:
    """feedIn extraction from the ElectricitySolar /Hourly response (#128).

    Fixture is a REAL anonymised capture (full local day 2026-07-01, AEST)
    provided on #128, reconciled against the AGL app for that same day:
    "Sold to Grid: 8.02 kWh ($1.36)", "Consumption: 6.07 kWh ($2.25)".
    These totals are the ground truth that validated reading the OUTER
    feedIn.quantity/amount — do not change them without a new capture.
    """

    def test_feedin_reconciles_with_agl_app_figures(self) -> None:
        data = load_fixture("solar_hourly_response.json")
        readings = parse_interval_readings(data, source_field="feedIn")
        # 11 daytime export slots survive; 37 night zero-on-zero slots drop.
        assert len(readings) == 11
        assert sum(r.kwh for r in readings) == pytest.approx(8.019)
        assert sum(r.cost_aud for r in readings) == pytest.approx(1.362924)

    def test_uses_outer_feedin_quantity_not_inner_values(self) -> None:
        """Inner values.* is the DPI/chart-scaled helper — it undercounts.

        On the 2026-07-01 capture the inner series sums to 6.1448 kWh vs the
        app-confirmed 8.02; reading it would repeat the v0.1.0 consumption
        undercount bug on the export side.
        """
        data = load_fixture("solar_hourly_response.json")
        readings = parse_interval_readings(data, source_field="feedIn")
        total = sum(r.kwh for r in readings)
        assert total != pytest.approx(6.1448)
        # Spot-check one slot: 01:00Z outer 1.305 kWh, inner 1.0.
        from datetime import UTC, datetime

        slot = {r.dt: r for r in readings}[datetime(2026, 7, 1, 1, 0, tzinfo=UTC)]
        assert slot.kwh == pytest.approx(1.305)
        assert slot.cost_aud == pytest.approx(0.2218)

    def test_feedin_carries_tou_rate_types(self) -> None:
        """Real solar responses type feedIn slots too (normal + peak seen)."""
        data = load_fixture("solar_hourly_response.json")
        all_types = {
            item["feedIn"]["type"]
            for section in data["sections"]
            for item in section["items"]
        }
        assert all_types == {"normal", "peak"}

    def test_consumption_side_of_solar_response_parses_with_default(self) -> None:
        """The solar response's consumption block matches the app figures.

        The coordinator ignores this block (the Electricity endpoint stays the
        consumption source of truth) but the reconciliation is documented:
        6.072 kWh / $2.2537 vs the app's 6.07 / $2.25 for 2026-07-01.
        """
        data = load_fixture("solar_hourly_response.json")
        readings = parse_interval_readings(data)
        assert len(readings) == 41
        assert sum(r.kwh for r in readings) == pytest.approx(6.072)
        assert sum(r.cost_aud for r in readings) == pytest.approx(2.253663)

    def test_feedin_filters_pending_and_none(self) -> None:
        """Synthetic payload — the real capture has no pending/none slots."""
        data = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "2026-07-01T04:00:00Z",
                            "feedIn": {
                                "amount": 0.1,
                                "quantity": 0.5,
                                "type": "pending",
                            },
                        },
                        {
                            "dateTime": "2026-07-01T04:30:00Z",
                            "feedIn": {"amount": 0.1, "quantity": 0.5, "type": "none"},
                        },
                        {
                            "dateTime": "2026-07-01T05:00:00Z",
                            "feedIn": {
                                "amount": 0.1,
                                "quantity": 0.5,
                                "type": "normal",
                            },
                        },
                    ]
                }
            ]
        }
        readings = parse_interval_readings(data, source_field="feedIn")
        assert len(readings) == 1
        assert readings[0].rate_type == "normal"


class TestParseOverviewSolar:
    def test_has_solar_true_on_solar_contract(self) -> None:
        data = load_fixture("overview_solar_response.json")
        contracts = parse_overview(data)
        assert len(contracts) == 1
        assert contracts[0].has_solar is True
        assert contracts[0].contract_number == "9999999999"

    def test_solar_contract_yields_no_bill_projection(self) -> None:
        """On a solar contract AGL puts "Sold To Grid" in the projection slot.

        Reading it positionally would publish feed-in credit as the bill
        projection — and since coordinator._money strips the "+", the result
        would be a PLAUSIBLE wrong number (7.43), not an obviously-broken one
        (#253; comment corrected per review — an earlier version claimed a
        $0.00 floor that does not exist).
        """
        contracts = parse_overview(load_fixture("overview_solar_response.json"))
        assert contracts[0].bill_projection_label == ""


# ---------------------------------------------------------------------------
# Totality (fuzz-enforced) — parsers must never raise on arbitrary JSON
# ---------------------------------------------------------------------------


class TestParserTotality:
    """Malformed/tampered JSON degrades to empty/default results, never raises.

    Response bodies are attacker-influenceable (TLS pinning is warn-only), so
    parser totality is a security invariant. The live enforcement is
    tests/fuzz/fuzz_parser.py; each case below is a crash class in the
    pre-hardened parser and pins the fix deterministically.
    """

    def test_bill_period_whitespace_quantity(self) -> None:
        # Was IndexError: "   ".split()[0] on an empty split result.
        data = {"billPeriod": {"current": {"usage": {"quantity": "   "}}}}
        assert parse_bill_period(data).consumption_kwh == 0.0

    def test_bill_period_numeric_quantity(self) -> None:
        # Was AttributeError: float has no .replace.
        data = {"billPeriod": {"current": {"usage": {"quantity": 42.5}}}}
        assert parse_bill_period(data).consumption_kwh == 42.5

    def test_bill_period_non_dict_nodes(self) -> None:
        # Was AttributeError: .get on list/str/int at every envelope level.
        for weird in (["x"], "str", 3, {"current": 7}, {"current": {"usage": []}}):
            bill = parse_bill_period({"billPeriod": weird})
            assert bill.consumption_kwh == 0.0
            assert bill.cost_label == "$0.00"

    def test_intervals_non_dict_sections_items_and_blocks(self) -> None:
        assert parse_interval_readings({"sections": 5}) == []
        assert parse_interval_readings({"sections": ["x", {"items": "y"}]}) == []
        data = {"sections": [{"items": ["x", {"consumption": "oops"}]}]}
        assert parse_interval_readings(data) == []

    def test_intervals_unhashable_type_skipped(self) -> None:
        # Was TypeError: unhashable dict in `rate_type in _skip_types`.
        data = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "2026-07-01T00:00:00Z",
                            "consumption": {"type": {}, "quantity": 1, "amount": 1},
                        }
                    ]
                }
            ]
        }
        assert parse_interval_readings(data) == []

    def test_daily_unhashable_type_keeps_original_keep_path(self) -> None:
        # Daily has no default type; absent/odd types were (and stay) kept —
        # only the unhashable-crash is fixed, not the keep semantics.
        data = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "2026-07-01T00:00:00Z",
                            "consumption": {"type": [], "quantity": 2, "amount": 3},
                        }
                    ]
                }
            ]
        }
        readings = parse_daily_readings(data)
        assert len(readings) == 1
        assert readings[0].kwh == 2.0

    def test_overview_malformed_contract_skipped_and_int_id_coerced(self) -> None:
        # Was KeyError on {} (missing contractNumber); junk entries crash-free.
        data = {
            "accounts": [
                "junk",
                {
                    "accountNumber": 12345,
                    "contracts": [{}, "junk", {"contractNumber": 999}],
                },
            ]
        }
        contracts = parse_overview(data)
        assert len(contracts) == 1
        assert contracts[0].contract_number == "999"
        assert contracts[0].account_number == "12345"

    def test_plan_non_dict_and_non_str_rows(self) -> None:
        # Was AttributeError: int .lower() via _classify_tariff on int titles,
        # and .get on non-dict rate rows.
        data = {
            "productName": 5,
            "gstInclusiveRates": ["x", {"kind": "header", "title": 7}, 3],
            "gstExclusiveRates": "nope",
        }
        plan = parse_plan(data)
        assert plan.product_name == ""
        assert plan.unit_rates == []
        assert plan.tou_unit_rates == {}
        assert plan.feed_in_rate_cents_per_kwh is None


# ---------------------------------------------------------------------------
# #292 — AGL Sydney-conversion correction
# ---------------------------------------------------------------------------

# API zone Sydney uses for all contracts regardless of contract state.
_SYD = ZoneInfo("Australia/Sydney")
# Contract zones used in vectors.
_ADL = ZoneInfo("Australia/Adelaide")
_BNE = ZoneInfo("Australia/Brisbane")
_SYD_CONTRACT = ZoneInfo("Australia/Sydney")
_BKH = ZoneInfo("Australia/Broken_Hill")


def _payload(
    dt_str: str,
    rate_type: str = "normal",
    kwh: float = 1.0,
    cost: float = 0.30,
    tz_field: str = "Australia/Sydney",
    source_field: str = "consumption",
) -> dict:
    """Minimal /Hourly-shaped payload for one slot."""
    block = {"type": rate_type, "quantity": kwh, "amount": cost}
    return {
        "timeZone": tz_field,
        "sections": [{"items": [{"dateTime": dt_str, source_field: block}]}],
    }


def _multi_payload(
    items: list[dict],
    tz_field: str = "Australia/Sydney",
    source_field: str = "consumption",
    kwh: float = 1.0,
    cost: float = 0.30,
) -> dict:
    """Payload with many slots, all the same kwh/cost."""
    built_items = []
    for item in items:
        block = {
            "type": item.get("rate_type", "normal"),
            "quantity": item.get("kwh", kwh),
            "amount": item.get("cost", cost),
        }
        built_items.append({"dateTime": item["dt"], source_field: block})
    return {
        "timeZone": tz_field,
        "sections": [{"items": built_items}],
    }


class TestRelocaliseAglTimestamp:
    """Unit tests for the relocalise_agl_timestamp public function (#292).

    Each test names the mutation that makes it red: remove the
    relocalise_agl_timestamp call → dt stays at the raw (wrong) value.
    """

    def test_adelaide_winter_acst_30min_correction(self) -> None:
        """SA ACST=UTC+9:30, Sydney AEST=UTC+10 → slot is 30 min early.

        AGL stores Adelaide midnight as 14:00Z (Sydney AEST midnight). The
        correction shifts it to 14:30Z (Adelaide ACST midnight).
        Mutation: remove call → stays at 14:00Z.
        """
        api_tz = _SYD
        dt = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
        got = relocalise_agl_timestamp(dt, api_tz=api_tz, contract_tz=_ADL)
        assert got == datetime(2026, 6, 1, 14, 30, tzinfo=UTC)

    def test_adelaide_summer_aedt_30min_correction(self) -> None:
        """SA ACDT=UTC+10:30, Sydney AEDT=UTC+11 → slot is still 30 min early.

        On 2026-10-04T13:00Z Sydney is on AEDT (DST started 2026-10-04 local).
        That instant is Sydney midnight of Oct 5 (00:00 AEDT). Adelaide is
        also on ACDT (+10:30) by then, so the net correction is 30 min.
        AGL stamps Adelaide midnight Oct 5 as 13:00Z; after correction: 13:30Z.
        (Reporter vector: 13:00Z→13:30Z post-DST per A8 example.)
        Mutation: remove call → stays at 13:00Z.
        """
        api_tz = _SYD
        dt = datetime(2026, 10, 4, 13, 0, tzinfo=UTC)
        got = relocalise_agl_timestamp(dt, api_tz=api_tz, contract_tz=_ADL)
        assert got == datetime(2026, 10, 4, 13, 30, tzinfo=UTC)

    def test_qld_winter_identity(self) -> None:
        """QLD is UTC+10 year-round. Sydney in AEST is also UTC+10 → no-op.

        Mutation: call with wrong contract_tz → would shift; identity proves
        the function doesn't over-correct.
        """
        api_tz = _SYD
        dt = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
        got = relocalise_agl_timestamp(dt, api_tz=api_tz, contract_tz=_BNE)
        assert got == dt

    def test_qld_summer_dst_one_hour_correction(self) -> None:
        """QLD UTC+10, Sydney AEDT=UTC+11 → slot is 1 h early.

        2026-10-04T13:00Z is Sydney midnight of Oct 5 in AEDT. AGL stores
        Brisbane midnight Oct 5 as 13:00Z; after correction: 14:00Z.
        (E1 confirmed from local QLD captures: D-1T13:00Z → DT12:30Z in AEDT
        season; the first slot midnight is 1 h early for Brisbane.)
        Mutation: remove call → stays at 13:00Z.
        """
        api_tz = _SYD
        dt = datetime(2026, 10, 4, 13, 0, tzinfo=UTC)
        got = relocalise_agl_timestamp(dt, api_tz=api_tz, contract_tz=_BNE)
        assert got == datetime(2026, 10, 4, 14, 0, tzinfo=UTC)

    def test_nsw_sydney_is_identity(self) -> None:
        """NSW/VIC/TAS/ACT share DST rules with Sydney → transform is identity."""
        api_tz = _SYD
        for dt_base in [
            datetime(2026, 6, 1, 14, 0, tzinfo=UTC),  # AEST
            datetime(2026, 10, 3, 13, 0, tzinfo=UTC),  # AEDT
            datetime(2026, 12, 1, 13, 30, tzinfo=UTC),  # AEDT mid-summer
        ]:
            got = relocalise_agl_timestamp(
                dt_base, api_tz=api_tz, contract_tz=_SYD_CONTRACT
            )
            assert got == dt_base, f"NSW should be identity for {dt_base}"

    def test_broken_hill_30min(self) -> None:
        """Broken Hill ACST=UTC+9:30 — same offset rules as Adelaide."""
        api_tz = _SYD
        dt = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
        got = relocalise_agl_timestamp(dt, api_tz=api_tz, contract_tz=_BKH)
        assert got == datetime(2026, 6, 1, 14, 30, tzinfo=UTC)

    def test_adelaide_dst_start_2026_10_04(self) -> None:
        """SA DST starts 2026-10-04 02:00 local (first Sunday of October) —
        the same morning Sydney springs forward (02:00 AEST → 03:00 AEDT).

        AGL labels every slot with its Sydney wall-clock time, so the inverse
        reads that label in Adelaide. Both zones skip 02:00-02:59 together,
        so the shift is +30 min on BOTH sides of the transition (ACST +9:30
        vs AEST +10 before, ACDT +10:30 vs AEDT +11 after); only the labels
        inside the skipped hour do not exist. Exact instants, not a
        monotonicity bound (`got >= dt` is satisfied by the identity).

        Mutation: transform removed (`return dt`) → every vector fails.
        """
        for raw, want in [
            # 01:30 AEST label → 01:30 ACST, the last pre-transition slot
            ("2026-10-03T15:30:00Z", "2026-10-03T16:00:00Z"),
            # 03:00 AEDT label → 03:00 ACDT, the first post-transition slot
            ("2026-10-03T16:00:00Z", "2026-10-03T16:30:00Z"),
            # next-day midnight: 00:00 AEDT label → 00:00 ACDT
            ("2026-10-04T13:00:00Z", "2026-10-04T13:30:00Z"),
            ("2026-10-04T13:30:00Z", "2026-10-04T14:00:00Z"),
        ]:
            got = relocalise_agl_timestamp(
                datetime.fromisoformat(raw), api_tz=_SYD, contract_tz=_ADL
            )
            assert got == datetime.fromisoformat(want), raw

    def test_adelaide_dst_end_2027_04_04_fold_0(self) -> None:
        """SA clocks fall back 2027-04-04 02:00 ACDT → 01:30 ACST.

        02:00 ACDT = 02:00 ACST exists twice; fold=0 is the DST side.
        Sydney also falls back the same morning (at 03:00 AEDT).
        Verify: the round-trip is the exact same instant (no hour lost).
        """
        # AEDT 02:00 on Sydney's fall-back day = 15:00Z (fold 0).
        api_tz = _SYD
        dt = datetime(2027, 4, 3, 15, 0, tzinfo=UTC)  # 02:00 AEDT fold-0
        got = relocalise_agl_timestamp(dt, api_tz=api_tz, contract_tz=_ADL)
        # Adelaide corrected: 02:00 AEDT wall → 02:00 ACDT → 15:30Z
        assert got == datetime(2027, 4, 3, 15, 30, tzinfo=UTC)

    def test_adelaide_dst_end_2027_04_04_fold_1(self) -> None:
        """Second occurrence of 02:00 on SA fall-back day (fold=1 = AEST)."""
        # AEST 02:00 on Sydney's fall-back day = 16:00Z (fold 1).
        api_tz = _SYD
        dt = datetime(2027, 4, 3, 16, 0, tzinfo=UTC)  # 02:00 AEST fold-1
        got = relocalise_agl_timestamp(dt, api_tz=api_tz, contract_tz=_ADL)
        # 02:00 AEST wall → 02:00 ACST (SA is already off DST) → 16:30Z
        assert got == datetime(2027, 4, 3, 16, 30, tzinfo=UTC)

    def test_brisbane_dst_end_2027_04_04_no_ambiguity(self) -> None:
        """Brisbane has no DST: Sydney's fold maps both labels to the same BNE instant.

        On Sydney's fall-back morning (Apr 4) the wall-clock reads "02:00"
        twice. Brisbane (UTC+10 fixed) has no ambiguity: both Sydney 02:00
        AEDT and 02:00 AEST read as 02:00 BNE but express different UTC:

        fold-0: 02:00 AEDT (15:00Z) → 02:00 BNE = 16:00Z (1 h correction)
        fold-1: 02:00 AEST (16:00Z) → 02:00 BNE = 16:00Z (identity — AEST=BNE)

        The key invariant: corrected >= raw for all AU zones.
        """
        api_tz = _SYD
        # fold-0: Sydney AEDT 02:00 = 15:00Z; BNE correction adds 1h.
        got0 = relocalise_agl_timestamp(
            datetime(2027, 4, 3, 15, 0, tzinfo=UTC), api_tz=api_tz, contract_tz=_BNE
        )
        assert got0 == datetime(2027, 4, 3, 16, 0, tzinfo=UTC)
        # fold-1: Sydney AEST 02:00 = 16:00Z; BNE is identity (both UTC+10).
        got1 = relocalise_agl_timestamp(
            datetime(2027, 4, 3, 16, 0, tzinfo=UTC), api_tz=api_tz, contract_tz=_BNE
        )
        assert got1 == datetime(2027, 4, 3, 16, 0, tzinfo=UTC)
        # Both folds now map to the same BNE instant (16:00Z). No ambiguity.
        assert got0 == got1

    def test_overflow_at_datetime_max_propagates(self) -> None:
        """astimezone at the edge of datetime range raises OverflowError.

        The caller (_IntervalTimestamps.parse) must catch it and drop the
        slot as malformed — this test verifies the exception propagates so
        the test for the caller can assert the drop.
        """
        # year=9999 + a large positive UTC offset may overflow.
        import pytest

        dt = datetime(9999, 12, 31, 23, 59, tzinfo=UTC)
        with pytest.raises((OverflowError, ValueError)):
            # astimezone can raise on the boundary; let the test prove the
            # contract and then the _IntervalTimestamps test proves the catch.
            relocalise_agl_timestamp(dt, api_tz=_SYD, contract_tz=_ADL)


class TestParseIntervalReadingsTzCorrection:
    """#292 integration tests for parse_interval_readings with tz_is_contract.

    Each test names the mutation that makes it red (per A10).
    """

    def test_adelaide_slot_corrected_not_dropped(self) -> None:
        """Core regression: SA first slot was dropped before fix (#292).

        2026-10-03 in Adelaide: local midnight is 14:30Z (ACST). AGL stamps
        the first slot as 14:00Z (Sydney AEDT midnight). Pre-fix: 14:00Z
        is before the strict lower bound 14:30Z → dropped + WARNING emitted.
        Post-fix (tz_is_contract): corrected to 14:30Z → passes window.

        Mutation: remove relocalise call → 14:00Z < 14:30Z → dropped.
        """
        payload = _payload("2026-10-02T14:00:00Z")
        result = parse_interval_readings(
            payload,
            expected_day=date(2026, 10, 3),
            tz=_ADL,
            tz_is_contract=True,
        )
        assert len(result) == 1
        assert result[0].dt == datetime(2026, 10, 2, 14, 30, tzinfo=UTC)

    def test_qld_summer_60min_corrected(self) -> None:
        """QLD in Sydney-AEDT season: first slot 1 h early.

        2026-10-04T13:00Z is Sydney midnight of Oct 5 (AEDT). Brisbane
        midnight Oct 5 is 14:00Z. AGL stamps it as 13:00Z.
        Post-fix: corrected to 14:00Z, within window [14:00Z, 14:00Z+26h).

        Mutation: remove relocalise → 13:00Z < 14:00Z → dropped.
        """
        payload = _payload("2026-10-04T13:00:00Z")
        result = parse_interval_readings(
            payload,
            expected_day=date(2026, 10, 5),
            tz=_BNE,
            tz_is_contract=True,
        )
        assert len(result) == 1
        assert result[0].dt == datetime(2026, 10, 4, 14, 0, tzinfo=UTC)

    def test_qld_summer_real_2026_capture_shape(self) -> None:
        """Local captures (evidence E1): Dec-Mar QLD days D-1T13:00Z -> DT12:30Z.

        For requested day D in Brisbane: AGL returns the first slot at
        (D-1)T13:00Z (Sydney AEDT midnight for D) and last slot at DT12:30Z.
        After correction both slots land in the Brisbane window
        [D-1T14:00Z, DT14:00Z + slack) and the Dropped WARNING must NOT fire.

        Mutation: tz_is_contract=False → correction off → first slot (13:00Z)
        dropped + WARNING.
        """
        two_slot = _multi_payload(
            [
                {"dt": "2026-10-04T13:00:00Z"},  # D-1 13:00Z (AEDT midnight D)
                {"dt": "2026-10-04T13:30:00Z"},  # D-1 13:30Z
            ]
        )
        result = parse_interval_readings(
            two_slot,
            expected_day=date(2026, 10, 5),
            tz=_BNE,
            tz_is_contract=True,
        )
        assert len(result) == 2
        assert result[0].dt == datetime(2026, 10, 4, 14, 0, tzinfo=UTC)
        assert result[1].dt == datetime(2026, 10, 4, 14, 30, tzinfo=UTC)

    def test_tz_is_contract_false_keeps_pre_fix_behaviour(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """HA-timezone fallback: correction OFF, dropped slot + WARNING (A3).

        tz_is_contract=False means the zone is not address-derived; the slot
        stays at the raw (wrong) timestamp, falls outside the window, and the
        existing WARNING fires.

        Mutation: apply correction when tz_is_contract=False → slot passes
        window silently even though the zone is wrong.
        """
        payload = _payload("2026-10-02T14:00:00Z")
        with caplog.at_level(
            logging.WARNING, logger="custom_components.haggle.agl.parser"
        ):
            result = parse_interval_readings(
                payload,
                expected_day=date(2026, 10, 3),
                tz=_ADL,
                tz_is_contract=False,
            )
        # Slot dropped: 14:00Z < Adelaide lower bound 14:30Z.
        assert len(result) == 0
        assert any("Dropped" in r.getMessage() for r in caplog.records)

    def test_tz_none_untouched(self) -> None:
        """No tz given: timestamps are unchanged regardless of tz_is_contract."""
        payload = _payload("2026-06-01T14:00:00Z")
        result = parse_interval_readings(
            payload,
            expected_day=date(2026, 6, 1),
            tz=None,
            tz_is_contract=True,
        )
        # The ±1-date coarse window admits 14:00Z for 2026-06-01.
        assert all(r.dt == datetime(2026, 6, 1, 14, 0, tzinfo=UTC) for r in result)

    def test_feedin_path_corrected(self) -> None:
        """feedIn source field goes through the same timestamp pipeline (#292).

        Mutation: apply correction to consumption but forget feedIn.
        """
        payload = _payload(
            "2026-10-02T14:00:00Z", source_field="feedIn", kwh=0.5, cost=0.10
        )
        result = parse_interval_readings(
            payload,
            source_field="feedIn",
            expected_day=date(2026, 10, 3),
            tz=_ADL,
            tz_is_contract=True,
        )
        assert len(result) == 1
        assert result[0].dt == datetime(2026, 10, 2, 14, 30, tzinfo=UTC)

    def test_api_tz_read_from_response_timezome_field(self) -> None:
        """api_tz comes from the response's timeZone, not from a hardcoded key.

        A payload declaring timeZone=Australia/Adelaide for an Adelaide
        contract: the inverse is Adelaide-through-Adelaide, an identity, so
        the 00:00 ACST slot (2026-10-02T14:30Z) must come back unchanged.
        The instant is chosen so that Sydney would disagree: via Sydney the
        same label is 00:30 AEST → relabelled to 15:00Z.

        Mutation: ignore the response timeZone, always use Sydney → the slot
        moves to 15:00Z and the assert fails. (A Brisbane-in-June vector
        could not tell the two apart — Sydney is also +10 then.)
        """
        payload = _payload("2026-10-02T14:30:00Z", tz_field="Australia/Adelaide")
        result = parse_interval_readings(
            payload,
            expected_day=date(2026, 10, 3),
            tz=_ADL,
            tz_is_contract=True,
        )
        assert len(result) == 1
        assert result[0].dt == datetime(2026, 10, 2, 14, 30, tzinfo=UTC)

    def test_missing_timezome_falls_back_to_sydney(self) -> None:
        """timeZone absent from response: fall back to AGL_API_TZ_KEY.

        The correction should still apply correctly (Sydney as default api_tz).
        Mutation: no-fallback → correction skipped entirely.
        """
        data_no_tz = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "2026-10-02T14:00:00Z",
                            "consumption": {
                                "type": "normal",
                                "quantity": 1.0,
                                "amount": 0.30,
                            },
                        }
                    ]
                }
            ]
        }
        result = parse_interval_readings(
            data_no_tz,
            expected_day=date(2026, 10, 3),
            tz=_ADL,
            tz_is_contract=True,
        )
        # Without the key the default (Sydney) is used → 14:00Z corrects to
        # 14:30Z for Adelaide, which passes the window.
        assert len(result) == 1
        assert result[0].dt == datetime(2026, 10, 2, 14, 30, tzinfo=UTC)

    def test_hostile_timezome_falls_back_to_sydney(self) -> None:
        """Untrusted timeZone values that can't be resolved → Sydney fallback.

        Mutation: don't bound-check → large key hits tzdata filesystem.
        """
        for bad_tz in ["", "Not/AZone", "../../etc/passwd", "x" * 100]:
            payload = _payload("2026-10-02T14:00:00Z", tz_field=bad_tz)
            # Must not raise; should still correct via Sydney default.
            result = parse_interval_readings(
                payload,
                expected_day=date(2026, 10, 3),
                tz=_ADL,
                tz_is_contract=True,
            )
            assert len(result) == 1, f"Expected 1 reading for bad_tz={bad_tz!r}"

    def test_utc_timezome_corrects_earlier_slot_caught_by_window(self) -> None:
        """timeZone='UTC' would shift Adelaide's slot to BEFORE local midnight.

        With contract_tz=Adelaide and api_tz=UTC the 'correction' moves the
        slot 9:30 h EARLIER, landing before the lower bound. The window check
        (which runs AFTER the correction) must drop it.

        Mutation: run window check BEFORE correction → slot at 14:00Z passes
        the coarse ±1-date window, reaching statistics with wrong timestamp.
        """
        _zone_for_key.cache_clear()
        _default_api_tz.cache_clear()
        payload = _payload("2026-10-02T14:00:00Z", tz_field="UTC")
        result = parse_interval_readings(
            payload,
            expected_day=date(2026, 10, 3),
            tz=_ADL,
            tz_is_contract=True,
        )
        # 14:00Z via UTC api_tz: wall-label = 14:00 "UTC", attach Adelaide
        # (+9:30) → 04:30 UTC, which is before 14:30Z (Adelaide lower bound).
        assert len(result) == 0

    def test_13_59z_still_dropped_no_leading_slack(self) -> None:
        """13:59Z is below Adelaide lower bound even after correction.

        The AGENTS.md rule 'never add leading slack' means the window lower
        bound is strict. A slot at 13:59Z corrects to 14:29Z (1 min before
        14:30Z Adelaide midnight) and must be dropped.

        Mutation: add leading slack → 13:59Z corrects to 14:29Z and passes
        if the window is 14:28Z or earlier.
        """
        payload = _payload("2026-10-02T13:59:00Z")
        result = parse_interval_readings(
            payload,
            expected_day=date(2026, 10, 3),
            tz=_ADL,
            tz_is_contract=True,
        )
        assert len(result) == 0

    def test_200_items_produces_exactly_one_debug_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DEBUG tripwire is bounded: one line per parse call, never per item.

        Mutation: log inside the loop → 200 DEBUG lines flood the log.
        """
        items = [
            {
                "dt": (
                    datetime(2026, 6, 1, 14, 0, tzinfo=UTC) + timedelta(minutes=30 * i)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            for i in range(200)
        ]
        payload = _multi_payload(items)
        with caplog.at_level(
            logging.DEBUG, logger="custom_components.haggle.agl.parser"
        ):
            parse_interval_readings(
                payload,
                expected_day=date(2026, 6, 1),
                tz=_BNE,
                tz_is_contract=True,
            )
        debug_lines = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "first raw dateTime" in r.getMessage()
        ]
        assert len(debug_lines) == 1

    def test_overflow_timestamp_dropped_as_malformed(self) -> None:
        """year-9999 slot near the datetime-max boundary is dropped, not raised.

        OverflowError from astimezone at the edge of the representable range
        must be caught in _IntervalTimestamps.parse.

        Mutation: let OverflowError propagate → parse_interval_readings raises
        instead of returning an empty list.

        Note: expected_day=date(9999, 12, 30) (not 31) because the window
        predicate computes expected_day+1 which overflows for Dec 31 of 9999.
        """
        payload = _payload("9999-12-31T22:00:00Z")
        # Must not raise — the overflow timestamp is dropped silently.
        result = parse_interval_readings(
            payload,
            expected_day=date(9998, 12, 31),
            tz=_ADL,
            tz_is_contract=True,
        )
        assert isinstance(result, list)

    def test_nsw_correction_is_identity_year_round(self) -> None:
        """NSW/VIC/TAS have the same DST rules as Sydney → zero net shift.

        Test multiple points: AEST, AEDT, and the transition boundary.
        Mutation: apply correction unconditionally → breaks for non-Sydney
        zones with different offsets.
        """
        nsw = ZoneInfo("Australia/Sydney")
        for dt_utc, expected_day_d in [
            (datetime(2026, 6, 1, 14, 0, tzinfo=UTC), date(2026, 6, 1)),
            (datetime(2026, 10, 5, 13, 0, tzinfo=UTC), date(2026, 10, 5)),
        ]:
            payload = _payload(dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
            result = parse_interval_readings(
                payload,
                expected_day=expected_day_d,
                tz=nsw,
                tz_is_contract=True,
            )
            assert len(result) == 1
            assert result[0].dt == dt_utc  # identity


class TestZoneForKey:
    """Defensive guards on _zone_for_key (A1 length-cap, ValueError catch)."""

    def test_valid_key_returns_zone(self) -> None:
        _zone_for_key.cache_clear()
        assert _zone_for_key("Australia/Sydney") is not None

    def test_empty_returns_none(self) -> None:
        _zone_for_key.cache_clear()
        assert _zone_for_key("") is None

    def test_too_long_returns_none(self) -> None:
        _zone_for_key.cache_clear()
        assert _zone_for_key("A" * 65) is None

    def test_unknown_key_returns_none(self) -> None:
        _zone_for_key.cache_clear()
        assert _zone_for_key("Not/AZone") is None

    def test_path_traversal_returns_none(self) -> None:
        _zone_for_key.cache_clear()
        assert _zone_for_key("../../etc/passwd") is None
