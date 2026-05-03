"""Tests for custom_components/haggle/agl/parser.py."""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, date

import pytest

from custom_components.haggle.agl.models import (
    BillPeriod,
    Contract,
    DailyReading,
    IntervalReading,
    PlanRates,
)
from custom_components.haggle.agl.parser import (
    parse_bill_period,
    parse_daily_readings,
    parse_interval_readings,
    parse_overview,
    parse_plan,
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
        from custom_components.haggle.agl.parser import _safe_float

        assert _safe_float(0.5) == 0.5
        assert _safe_float("12.34") == pytest.approx(12.34)

    def test_inf_nan_negative_clamp_to_zero(self) -> None:
        from custom_components.haggle.agl.parser import _safe_float

        assert _safe_float(float("inf")) == 0.0
        assert _safe_float(float("nan")) == 0.0
        assert _safe_float(-1.0) == 0.0
        assert _safe_float("1e308") == pytest.approx(1e308)  # finite, allowed
        assert _safe_float(1e400) == 0.0  # overflow → inf → clamped

    def test_unparseable_clamps_to_zero(self) -> None:
        from custom_components.haggle.agl.parser import _safe_float

        assert _safe_float(None) == 0.0
        assert _safe_float("not a number") == 0.0
        assert _safe_float({}) == 0.0


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

    def test_uses_values_quantity_not_top_level_quantity(self) -> None:
        """kWh must come from consumption.values.quantity, not consumption.quantity.

        The fixture has values.quantity=0.112 but quantity=0.175 for the first
        item — we must get 0.112.
        """
        data = load_fixture("hourly_response.json")
        readings = parse_interval_readings(data)
        kwhs = {r.kwh for r in readings}
        # These are values.quantity values from the fixture
        assert 0.112 in kwhs
        assert 0.119 in kwhs
        # The rounded top-level quantities must NOT appear
        assert 0.175 not in kwhs
        assert 0.186 not in kwhs

    def test_dt_is_tz_aware_utc(self) -> None:
        """Every parsed datetime must be UTC-aware."""
        data = load_fixture("hourly_response.json")
        readings = parse_interval_readings(data)
        assert len(readings) > 0
        for r in readings:
            assert r.dt.tzinfo is not None
            assert r.dt.tzinfo == UTC

    def test_expected_count_after_none_filter(self) -> None:
        """Fixture has 7 items, 1 with type=none → 6 readings returned.

        6 = 4 normal + 1 peak + 1 normal. The none slot is filtered out.
        """
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
        assert peak_readings[0].kwh == pytest.approx(0.675)

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
                                "values": {"quantity": 0.5},
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


# ---------------------------------------------------------------------------
# parse_overview
# ---------------------------------------------------------------------------


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
                                "values": {"quantity": 5.2},
                                "amount": 1.75,
                                "type": "normal",
                            },
                        },
                        {
                            "dateTime": "2026-04-29T00:00:00Z",
                            "consumption": {
                                "values": {"quantity": 0.0},
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
                                "values": {"quantity": 5.2},
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

    def test_daily_uses_values_quantity(self) -> None:
        data = {
            "sections": [
                {
                    "items": [
                        {
                            "dateTime": "2026-04-28T00:00:00Z",
                            "consumption": {
                                "values": {"quantity": 5.2},
                                "amount": 1.75,
                                "type": "normal",
                            },
                        }
                    ]
                }
            ]
        }
        readings = parse_daily_readings(data)
        assert readings[0].kwh == pytest.approx(5.2)
