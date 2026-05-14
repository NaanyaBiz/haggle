"""Derived metrics: bill projection, period comparisons, tariff bands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import storage

# Brisbane / Sydney = UTC+10 (no DST in QLD; NSW does DST). Configurable via
# HAGGLE_TZ_OFFSET env (minutes, e.g. 600 for AEST).
import os

TZ_OFFSET_MINUTES = int(os.environ.get("HAGGLE_TZ_OFFSET", "600"))
# AGL meter data is delayed by AEMO's feed; "today" and "yesterday" are usually
# empty placeholders. Default 2 days; bump if your distributor lags more.
DATA_LAG_DAYS = int(os.environ.get("HAGGLE_DATA_LAG_DAYS", "2"))


@dataclass(slots=True)
class BillProjection:
    period_start: date
    period_end: date
    days_elapsed: int
    days_total: int
    consumption_kwh_to_date: float
    cost_aud_to_date: float
    avg_daily_kwh: float
    avg_daily_cost_aud: float
    projected_kwh: float
    projected_cost_aud: float
    supply_charge_total_aud: float
    export_kwh_to_date: float
    credit_aud_to_date: float
    avg_daily_export_kwh: float
    projected_export_kwh: float
    projected_credit_aud: float
    net_cost_to_date: float
    projected_net_cost_aud: float
    self_consumption_ratio: float | None
    agl_label_cost: str | None
    agl_label_projection: str | None


def bill_projection(contract_number: str) -> BillProjection | None:
    bp = storage.get_bill_period(contract_number)
    if not bp:
        return None
    try:
        start = date.fromisoformat(bp["start_date"])
        end = date.fromisoformat(bp["end_date"])
    except (TypeError, ValueError):
        return None

    today_local = (datetime.now(UTC) + timedelta(minutes=TZ_OFFSET_MINUTES)).date()
    days_total = max((end - start).days + 1, 1)
    days_elapsed = max(min((today_local - start).days, days_total), 1)

    daily = storage.daily_totals(contract_number, start, today_local, TZ_OFFSET_MINUTES)
    kwh_to_date = sum(d["kwh"] for d in daily)
    cost_to_date_usage = sum(d["cost_aud"] for d in daily)
    export_to_date = sum(d.get("kwh_export", 0.0) for d in daily)
    credit_to_date = sum(d.get("credit_aud", 0.0) for d in daily)

    plan = storage.get_plan(contract_number)
    supply_per_day = (plan or {}).get("supply_charge_aud_per_day") or 0.0
    supply_to_date = supply_per_day * days_elapsed
    supply_total = supply_per_day * days_total

    cost_to_date = cost_to_date_usage + supply_to_date
    net_cost_to_date = cost_to_date - credit_to_date
    # AGL data lags AEMO by ~2 days; the most recent days_elapsed days don't
    # all have data yet. Average over actual data days so projections aren't
    # dragged down by zero-padded tail.
    days_with_data = max(days_elapsed - DATA_LAG_DAYS, 1)
    avg_daily_kwh = kwh_to_date / days_with_data
    avg_daily_cost_usage = cost_to_date_usage / days_with_data
    avg_daily_export = export_to_date / days_with_data
    avg_daily_credit = credit_to_date / days_with_data
    projected_kwh = avg_daily_kwh * days_total
    projected_cost_usage = avg_daily_cost_usage * days_total
    projected_cost = projected_cost_usage + supply_total
    projected_export = avg_daily_export * days_total
    projected_credit = avg_daily_credit * days_total
    projected_net = projected_cost - projected_credit

    # Self-consumption: how much solar generation was used on-site vs exported.
    # We only have grid imports + grid exports, so "generation" here is implied;
    # leave None unless we can prove a denominator > 0.
    self_consumption_ratio = None
    if export_to_date > 0 and kwh_to_date > 0:
        # Rough heuristic: solar offset = (export + assumed self-use). Without
        # a dedicated generation channel from AGL we can't compute true ratio,
        # so we expose export_per_consumption instead as a proxy in the UI.
        self_consumption_ratio = None

    return BillProjection(
        period_start=start,
        period_end=end,
        days_elapsed=days_elapsed,
        days_total=days_total,
        consumption_kwh_to_date=kwh_to_date,
        cost_aud_to_date=cost_to_date,
        avg_daily_kwh=avg_daily_kwh,
        avg_daily_cost_aud=avg_daily_cost_usage + supply_per_day,
        projected_kwh=projected_kwh,
        projected_cost_aud=projected_cost,
        supply_charge_total_aud=supply_total,
        export_kwh_to_date=export_to_date,
        credit_aud_to_date=credit_to_date,
        avg_daily_export_kwh=avg_daily_export,
        projected_export_kwh=projected_export,
        projected_credit_aud=projected_credit,
        net_cost_to_date=net_cost_to_date,
        projected_net_cost_aud=projected_net,
        self_consumption_ratio=self_consumption_ratio,
        agl_label_cost=bp.get("cost_label"),
        agl_label_projection=bp.get("projection_label"),
    )


def comparisons(contract_number: str) -> dict[str, Any]:
    """Week-over-week, month-over-month, year-over-year totals.

    All windows are anchored against `today - DATA_LAG_DAYS` so we compare
    fully-populated periods, not periods that include the AEMO-lag tail of
    zero/placeholder days.
    """
    today_local = (datetime.now(UTC) + timedelta(minutes=TZ_OFFSET_MINUTES)).date()
    anchor = today_local - timedelta(days=DATA_LAG_DAYS)

    def _window(days: int, offset_days: int = 0) -> dict[str, float]:
        end = anchor - timedelta(days=offset_days)
        start = end - timedelta(days=days - 1)
        rows = storage.daily_totals(contract_number, start, end, TZ_OFFSET_MINUTES)
        return {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "kwh": sum(r["kwh"] for r in rows),
            "cost_aud": sum(r["cost_aud"] for r in rows),
            "kwh_export": sum(r.get("kwh_export", 0.0) for r in rows),
            "credit_aud": sum(r.get("credit_aud", 0.0) for r in rows),
            "days": days,
        }

    return {
        "week": {
            "current": _window(7, 0),
            "previous": _window(7, 7),
            "year_ago": _window(7, 365),
        },
        "month": {
            "current": _window(30, 0),
            "previous": _window(30, 30),
            "year_ago": _window(30, 365),
        },
    }


def heatmap(contract_number: str, weeks_back: int = 8) -> dict[str, Any]:
    """Half-hourly average kWh by (weekday, slot) for the trailing window."""
    today_local = (datetime.now(UTC) + timedelta(minutes=TZ_OFFSET_MINUTES)).date()
    start = today_local - timedelta(days=weeks_back * 7)
    start_utc = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    end_utc = datetime.combine(today_local + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    rows = storage.fetch_intervals(contract_number, start_utc, end_utc)

    sums: dict[tuple[int, int], float] = {}
    counts: dict[tuple[int, int], int] = {}
    for r in rows:
        dt_local = datetime.fromisoformat(r["ts"]) + timedelta(minutes=TZ_OFFSET_MINUTES)
        weekday = dt_local.weekday()  # 0=Mon
        slot = dt_local.hour * 2 + (1 if dt_local.minute >= 30 else 0)
        key = (weekday, slot)
        sums[key] = sums.get(key, 0.0) + r["kwh"]
        counts[key] = counts.get(key, 0) + 1

    cells = []
    for wd in range(7):
        for slot in range(48):
            n = counts.get((wd, slot), 0)
            avg = sums.get((wd, slot), 0.0) / n if n else 0.0
            cells.append({"weekday": wd, "slot": slot, "kwh": avg, "n": n})
    return {"window_start": start.isoformat(), "weeks": weeks_back, "cells": cells}
