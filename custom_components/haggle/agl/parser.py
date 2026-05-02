"""Parsers: AGL JSON API responses -> domain dataclasses.

Reference response shapes are in ~/tests/fixtures/flows/agl-json/.
Reference: ~/tests/fixtures/AGL-API-FINDINGS.md section 2.

Critical fields:
  - Interval kWh: consumption.values.quantity  (NOT consumption.quantity)
  - Interval dt:  dateTime field is slot-start in UTC
  - Contract ID:  contractNumber (not accountNumber, not accountId)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from .models import BillPeriod, Contract, DailyReading, IntervalReading, PlanRates


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

    Filters out items with type='none' (future/unavailable slots).
    dateTime is slot-start UTC; kwh from consumption.values.quantity.
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
            values = consumption.get("values") or {}
            kwh: float = float(values.get("quantity") or 0.0)
            cost_aud: float = float(consumption.get("amount") or 0.0)
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
    kWh from consumption.values.quantity.
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
            values = consumption.get("values") or {}
            kwh: float = float(values.get("quantity") or 0.0)
            cost_aud: float = float(consumption.get("amount") or 0.0)
            readings.append(DailyReading(day=day, kwh=kwh, cost_aud=cost_aud))
    return readings


def parse_bill_period(data: dict[str, Any]) -> BillPeriod:
    """Parse /usage summary response.

    Path: data["billPeriod"]["current"].
    """
    current = (data.get("billPeriod") or {}).get("current") or {}

    start_str: str = (current.get("start") or {}).get("date", "")
    end_str: str = (current.get("end") or {}).get("date", "")
    try:
        start = date.fromisoformat(start_str)
    except (ValueError, TypeError):
        start = date.today()
    try:
        end = date.fromisoformat(end_str)
    except (ValueError, TypeError):
        end = date.today()

    usage = current.get("usage") or {}
    cost_label: str = usage.get("amount", "$0.00")

    # projection is in the overview response but not in the usage summary;
    # return empty string if absent — callers can populate from overview.
    projection_label: str = data.get("additionalLabelValue", "")

    quantity_str: str = (usage.get("quantity") or "0").replace(",", "").split()[0]
    try:
        consumption_kwh: float = float(quantity_str)
    except (ValueError, IndexError):
        consumption_kwh = 0.0

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
        price: float = float(rate.get("price") or 0.0)
        if rate_type == "c/day" and "supply" in (rate.get("title") or "").lower():
            supply_charge = price
        unit_rates.append(dict(rate))

    return PlanRates(
        product_name=product_name,
        unit_rates=unit_rates,
        supply_charge_cents_per_day=supply_charge,
    )
