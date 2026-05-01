"""Domain dataclasses for the AGL API layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import date, datetime


@dataclass(slots=True)
class TokenSet:
    """Holds a live access token + the current (rotated) refresh token."""

    access_token: str
    refresh_token: str  # MUST be persisted after every rotation
    expires_at: datetime  # UTC; derived from `expires_in`
    id_token: str = ""  # for identity claims if needed


@dataclass(slots=True)
class Contract:
    """One fuel/service contract from /api/v3/overview."""

    contract_number: str
    account_number: str
    address: str
    fuel_type: str  # "electricityContract" | "gasContract"
    status: str  # "active" | ...
    has_solar: bool = False
    meter_type: str = "smart"


@dataclass(slots=True)
class IntervalReading:
    """One 30-minute interval reading from /Hourly."""

    dt: datetime  # slot start, UTC
    kwh: float  # consumption.values.quantity — source of truth
    cost_aud: float  # consumption.amount
    rate_type: str  # "normal" | "peak" | "offpeak" | "shoulder" | "none"


@dataclass(slots=True)
class DailyReading:
    """One day aggregate from /Daily."""

    day: date
    kwh: float
    cost_aud: float


@dataclass(slots=True)
class BillPeriod:
    """Current bill period boundaries + totals from /usage summary."""

    start: date
    end: date
    consumption_kwh: float
    cost_label: str  # e.g. "$87.38"
    projection_label: str  # e.g. "$139.15"


@dataclass(slots=True)
class PlanRates:
    """Tariff rates from /api/v2/plan/energy/{contractNumber}."""

    product_name: str
    unit_rates: list[dict[str, Any]] = field(default_factory=list)
    supply_charge_cents_per_day: float = 0.0
