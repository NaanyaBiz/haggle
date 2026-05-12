"""Parsers: AGL JSON API responses -> domain dataclasses.

Reference response shapes are mirrored under tests/fixtures/ (anonymised).
Field semantics are documented in AGENTS.md §AGL API — Key Facts.

Critical fields:
  - Interval kWh:  consumption.quantity        (outer — matches AEMO/CSV)
  - Interval cost: consumption.amount          (outer — AUD for the slot)
  - Interval dt:   dateTime field is slot-start in UTC
  - Contract ID:   contractNumber (not accountNumber, not accountId)

The inner ``consumption.values.{amount,quantity}`` block is a DPI/chart-scaled
helper (the two sub-fields are always equal in real responses) and is NOT
the metered value. Reading it undercounts kWh by 4-73% with no consistent
ratio — confirmed by reconciling 11 mitm /Hourly captures against an AGL
"MyUsageData" portal CSV export, 2026-05-12.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime
from typing import Any

from .models import BillPeriod, Contract, DailyReading, IntervalReading, PlanRates

_LOGGER = logging.getLogger(__name__)


def _safe_float(raw: Any) -> float:
    """Coerce raw API value to a non-negative finite float.

    Treats inf/nan/negative as 0.0 with a warning, so adversarial or corrupt
    AGL responses cannot poison the recorder via async_add_external_statistics.
    """
    try:
        value = float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value < 0:
        _LOGGER.warning("Rejecting non-finite/negative AGL value: %r", raw)
        return 0.0
    return value


def parse_overview(data: dict[str, Any]) -> list[Contract]:
    """Parse /api/v3/overview response."""
    contracts: list[Contract] = []
    for account in data.get("accounts") or []:
        account_number: str = account.get("accountNumber", "")
        address: str = account.get("address", "")
        for c in account.get("contracts") or []:
            contracts.append(
                Contract(
                    contract_number=c["contractNumber"],
                    account_number=account_number,
                    address=address,
                    fuel_type=c.get("type", ""),
                    status=c.get("status", ""),
                    has_solar=bool(c.get("hasSolar", False)),
                    meter_type=c.get("meterType", "smart"),
                )
            )
    return contracts


def parse_interval_readings(data: dict[str, Any]) -> list[IntervalReading]:
    """Parse /Hourly response into 30-min interval readings.

    Filters out items with type='none' (future/unavailable slots) and
    placeholder slots where kWh and cost are both zero (AGL returns these
    for days where AEMO meter reads have not yet been delivered, even with
    a non-``none`` type — they would otherwise create phantom flat rows in
    the statistics table that the resume logic would never re-check).
    dateTime is slot-start UTC; kwh from consumption.quantity (outer).
    """
    readings: list[IntervalReading] = []
    for section in data.get("sections") or []:
        for item in section.get("items") or []:
            consumption = item.get("consumption") or {}
            rate_type: str = consumption.get("type", "none")
            if rate_type == "none":
                continue
            dt_str: str = item.get("dateTime", "")
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
            except (ValueError, AttributeError):
                continue
            kwh = _safe_float(consumption.get("quantity"))
            cost_aud = _safe_float(consumption.get("amount"))
            if kwh == 0.0 and cost_aud == 0.0:
                continue
            readings.append(
                IntervalReading(
                    dt=dt,
                    kwh=kwh,
                    cost_aud=cost_aud,
                    rate_type=rate_type,
                )
            )
    return readings


def parse_daily_readings(data: dict[str, Any]) -> list[DailyReading]:
    """Parse /Daily response.

    Response uses sections[].items[] — same envelope as /Hourly.
    dateTime is day-start in UTC (time component is 00:00:00Z).
    kWh from consumption.quantity (outer — see module docstring).
    """
    readings: list[DailyReading] = []
    for section in data.get("sections") or []:
        for item in section.get("items") or []:
            consumption = item.get("consumption") or {}
            if consumption.get("type") == "none":
                continue
            dt_str: str = item.get("dateTime", "")
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                day: date = dt.date()
            except (ValueError, AttributeError):
                continue
            kwh = _safe_float(consumption.get("quantity"))
            cost_aud = _safe_float(consumption.get("amount"))
            if kwh == 0.0 and cost_aud == 0.0:
                continue
            readings.append(DailyReading(day=day, kwh=kwh, cost_aud=cost_aud))
    return readings


def parse_bill_period(data: dict[str, Any]) -> BillPeriod:
    """Parse /usage summary response.

    Path: data["billPeriod"]["current"].
    """
    current = (data.get("billPeriod") or {}).get("current") or {}

    start_str: str = (current.get("start") or {}).get("date", "")
    end_str: str = (current.get("end") or {}).get("date", "")
    today_utc = datetime.now(UTC).date()
    try:
        start = date.fromisoformat(start_str)
    except (ValueError, TypeError):
        start = today_utc
    try:
        end = date.fromisoformat(end_str)
    except (ValueError, TypeError):
        end = today_utc

    usage = current.get("usage") or {}
    cost_label: str = usage.get("amount", "$0.00")

    # projection is in the overview response but not in the usage summary;
    # return empty string if absent — callers can populate from overview.
    projection_label: str = data.get("additionalLabelValue", "")

    quantity_str: str = (usage.get("quantity") or "0").replace(",", "").split()[0]
    consumption_kwh = _safe_float(quantity_str)

    return BillPeriod(
        start=start,
        end=end,
        consumption_kwh=consumption_kwh,
        cost_label=cost_label,
        projection_label=projection_label,
    )


def parse_plan(data: dict[str, Any]) -> PlanRates:
    """Parse /api/v2/plan/energy/{contractNumber} response."""
    product_name: str = data.get("productName", "")
    unit_rates: list[dict[str, Any]] = []
    supply_charge: float = 0.0

    for rate in data.get("gstInclusiveRates") or []:
        if rate.get("kind") != "detail":
            continue
        rate_type: str = rate.get("type", "")
        price = _safe_float(rate.get("price"))
        if rate_type == "c/day" and "supply" in (rate.get("title") or "").lower():
            supply_charge = price
        # Allowlist the four fields the coordinator actually consumes — drops
        # any extra keys an attacker-controlled (MITM) response could inject.
        unit_rates.append(
            {
                "kind": rate.get("kind"),
                "type": rate_type,
                "title": rate.get("title"),
                "price": price,
            }
        )

    return PlanRates(
        product_name=product_name,
        unit_rates=unit_rates,
        supply_charge_cents_per_day=supply_charge,
    )
