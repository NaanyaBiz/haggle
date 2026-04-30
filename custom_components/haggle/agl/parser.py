"""Parsers: AGL JSON API responses -> domain dataclasses.

Stub. Real implementation is Sprint 1.
Reference response shapes are in ~/scratch/aglreversing/flows/agl-json/.
Reference: ~/scratch/aglreversing/AGL-API-FINDINGS.md section 2.

Critical fields:
  - Interval kWh: consumption.values.quantity  (NOT consumption.quantity)
  - Interval dt:  dateTime field is slot-start in UTC
  - Contract ID:  contractNumber (not accountNumber, not accountId)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import BillPeriod, Contract, DailyReading, IntervalReading


def parse_overview(data: dict[str, Any]) -> list[Contract]:
    """Parse /api/v3/overview response."""
    raise NotImplementedError("agl-api: implement in Sprint 1")


def parse_interval_readings(data: dict[str, Any]) -> list[IntervalReading]:
    """Parse /Hourly response into 30-min interval readings.

    Filters out items with type='none' (future/unavailable slots).
    dateTime is slot-start UTC; kwh from consumption.values.quantity.
    """
    raise NotImplementedError("agl-api: implement in Sprint 1")


def parse_daily_readings(data: dict[str, Any]) -> list[DailyReading]:
    """Parse /Daily response."""
    raise NotImplementedError("agl-api: implement in Sprint 1")


def parse_bill_period(data: dict[str, Any]) -> BillPeriod:
    """Parse /usage summary response."""
    raise NotImplementedError("agl-api: implement in Sprint 1")
