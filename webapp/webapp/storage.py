"""SQLite-backed storage for the haggle webapp.

One file (haggle.db) holds:
  - config       — refresh_token, contract_number, account_number, TOFU pins
  - contracts    — discovered AGL contracts
  - intervals    — 30-min readings (kwh, cost, rate_type) keyed (contract, ts_utc)
  - plan         — latest tariff snapshot per contract
  - bill_period  — latest bill-period snapshot per contract

All writes are wrapped in a transaction; intervals upsert by primary key so
the trailing-rewindow self-heal is idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

DB_PATH_ENV = "HAGGLE_DB"
DEFAULT_DB_PATH = Path.home() / ".haggle" / "haggle.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    contract_number TEXT PRIMARY KEY,
    account_number  TEXT,
    address         TEXT,
    fuel_type       TEXT,
    status          TEXT,
    has_solar       INTEGER DEFAULT 0,
    discovered_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intervals (
    contract_number TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    kwh             REAL NOT NULL,
    cost_aud        REAL NOT NULL,
    rate_type       TEXT NOT NULL,
    kwh_export      REAL NOT NULL DEFAULT 0,
    credit_aud      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (contract_number, ts_utc)
);

CREATE INDEX IF NOT EXISTS idx_intervals_ts
    ON intervals(contract_number, ts_utc);

CREATE TABLE IF NOT EXISTS plan (
    contract_number             TEXT PRIMARY KEY,
    fetched_at                  TEXT NOT NULL,
    product_name                TEXT,
    supply_charge_aud_per_day   REAL,
    unit_rates_json             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bill_period (
    contract_number     TEXT PRIMARY KEY,
    fetched_at          TEXT NOT NULL,
    start_date          TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    consumption_kwh     REAL,
    cost_label          TEXT,
    projection_label    TEXT
);
"""


def db_path() -> Path:
    import os

    raw = os.environ.get(DB_PATH_ENV)
    return Path(raw) if raw else DEFAULT_DB_PATH


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema. Idempotent."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(intervals)")}
    if "kwh_export" not in cols:
        c.execute("ALTER TABLE intervals ADD COLUMN kwh_export REAL NOT NULL DEFAULT 0")
    if "credit_aud" not in cols:
        c.execute("ALTER TABLE intervals ADD COLUMN credit_aud REAL NOT NULL DEFAULT 0")


# ---------------------------------------------------------------------------
# Config (refresh_token + chosen contract + TOFU SPKI pins)
# ---------------------------------------------------------------------------


def get_config(key: str) -> str | None:
    with connect() as c:
        row = c.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_config(key: str, value: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def all_config() -> dict[str, str]:
    with connect() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM config")}


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StoredContract:
    contract_number: str
    account_number: str
    address: str
    fuel_type: str
    status: str
    has_solar: bool


def upsert_contracts(contracts: Iterable[Any]) -> None:
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        for k in contracts:
            c.execute(
                "INSERT INTO contracts(contract_number, account_number, address, "
                "fuel_type, status, has_solar, discovered_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(contract_number) DO UPDATE SET "
                "address=excluded.address, status=excluded.status, "
                "has_solar=excluded.has_solar",
                (
                    k.contract_number,
                    k.account_number,
                    k.address,
                    k.fuel_type,
                    k.status,
                    1 if k.has_solar else 0,
                    now,
                ),
            )


def list_contracts() -> list[StoredContract]:
    with connect() as c:
        return [
            StoredContract(
                contract_number=r["contract_number"],
                account_number=r["account_number"] or "",
                address=r["address"] or "",
                fuel_type=r["fuel_type"] or "",
                status=r["status"] or "",
                has_solar=bool(r["has_solar"]),
            )
            for r in c.execute("SELECT * FROM contracts ORDER BY contract_number")
        ]


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------


def upsert_intervals(contract_number: str, readings: Iterable[Any]) -> int:
    """Insert/replace 30-min readings. Returns number of rows touched.

    Accepts either dicts (from parser_solar) or objects with attribute access
    (legacy IntervalReading). Missing kwh_export/credit_aud default to 0.0
    so non-solar accounts work unchanged.
    """

    def _g(r: Any, key: str, default: Any = None) -> Any:
        if isinstance(r, dict):
            return r.get(key, default)
        return getattr(r, key, default)

    rows = []
    for r in readings:
        dt = _g(r, "dt")
        rows.append(
            (
                contract_number,
                dt.astimezone(UTC).isoformat(),
                float(_g(r, "kwh", 0.0) or 0.0),
                float(_g(r, "cost_aud", 0.0) or 0.0),
                _g(r, "rate_type", "normal"),
                float(_g(r, "kwh_export", 0.0) or 0.0),
                float(_g(r, "credit_aud", 0.0) or 0.0),
            )
        )
    if not rows:
        return 0
    with connect() as c:
        c.executemany(
            "INSERT INTO intervals(contract_number, ts_utc, kwh, cost_aud, "
            "rate_type, kwh_export, credit_aud) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(contract_number, ts_utc) DO UPDATE SET "
            "kwh=excluded.kwh, cost_aud=excluded.cost_aud, "
            "rate_type=excluded.rate_type, "
            "kwh_export=excluded.kwh_export, credit_aud=excluded.credit_aud",
            rows,
        )
    return len(rows)


def has_any_export(contract_number: str) -> bool:
    """Return True if we've ever stored a non-zero kwh_export for this contract."""
    with connect() as c:
        row = c.execute(
            "SELECT 1 FROM intervals WHERE contract_number = ? "
            "AND kwh_export > 0 LIMIT 1",
            (contract_number,),
        ).fetchone()
        return row is not None


def latest_interval_date(contract_number: str) -> date | None:
    with connect() as c:
        row = c.execute(
            "SELECT MAX(ts_utc) AS m FROM intervals WHERE contract_number = ?",
            (contract_number,),
        ).fetchone()
        if not row or not row["m"]:
            return None
        return datetime.fromisoformat(row["m"]).date()


def earliest_interval_date(contract_number: str) -> date | None:
    with connect() as c:
        row = c.execute(
            "SELECT MIN(ts_utc) AS m FROM intervals WHERE contract_number = ?",
            (contract_number,),
        ).fetchone()
        if not row or not row["m"]:
            return None
        return datetime.fromisoformat(row["m"]).date()


def fetch_intervals(
    contract_number: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute(
            "SELECT ts_utc, kwh, cost_aud, rate_type, kwh_export, credit_aud "
            "FROM intervals WHERE contract_number = ? "
            "AND ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc",
            (contract_number, start_utc.isoformat(), end_utc.isoformat()),
        ).fetchall()
    return [
        {
            "ts": r["ts_utc"],
            "kwh": r["kwh"],
            "cost_aud": r["cost_aud"],
            "rate_type": r["rate_type"],
            "kwh_export": r["kwh_export"] or 0.0,
            "credit_aud": r["credit_aud"] or 0.0,
        }
        for r in rows
    ]


def daily_totals(
    contract_number: str,
    from_date: date,
    to_date: date,
    tz_offset_minutes: int = 0,
) -> list[dict[str, Any]]:
    """Aggregate 30-min intervals into local-day totals.

    `tz_offset_minutes` shifts UTC timestamps before bucketing so a Brisbane
    user (UTC+10 = 600) sees consumption attributed to the local day on which
    it occurred.
    """
    with connect() as c:
        rows = c.execute(
            "SELECT ts_utc, kwh, cost_aud, kwh_export, credit_aud FROM intervals "
            "WHERE contract_number = ? AND ts_utc >= ? AND ts_utc < ? "
            "ORDER BY ts_utc",
            (
                contract_number,
                datetime.combine(from_date, datetime.min.time(), tzinfo=UTC).isoformat(),
                datetime.combine(
                    to_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC
                ).isoformat(),
            ),
        ).fetchall()

    buckets: dict[str, dict[str, float]] = {}
    for r in rows:
        dt_utc = datetime.fromisoformat(r["ts_utc"])
        dt_local = dt_utc + timedelta(minutes=tz_offset_minutes)
        day_key = dt_local.date().isoformat()
        b = buckets.setdefault(
            day_key,
            {"kwh": 0.0, "cost_aud": 0.0, "kwh_export": 0.0, "credit_aud": 0.0},
        )
        b["kwh"] += float(r["kwh"])
        b["cost_aud"] += float(r["cost_aud"])
        b["kwh_export"] += float(r["kwh_export"] or 0.0)
        b["credit_aud"] += float(r["credit_aud"] or 0.0)

    return [
        {
            "day": d,
            "kwh": v["kwh"],
            "cost_aud": v["cost_aud"],
            "kwh_export": v["kwh_export"],
            "credit_aud": v["credit_aud"],
        }
        for d, v in sorted(buckets.items())
    ]


# ---------------------------------------------------------------------------
# Plan + bill period snapshots
# ---------------------------------------------------------------------------


def save_plan(contract_number: str, plan: Any) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO plan(contract_number, fetched_at, product_name, "
            "supply_charge_aud_per_day, unit_rates_json) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(contract_number) DO UPDATE SET "
            "fetched_at=excluded.fetched_at, product_name=excluded.product_name, "
            "supply_charge_aud_per_day=excluded.supply_charge_aud_per_day, "
            "unit_rates_json=excluded.unit_rates_json",
            (
                contract_number,
                datetime.now(UTC).isoformat(),
                plan.product_name,
                plan.supply_charge_cents_per_day / 100.0,
                json.dumps(plan.unit_rates),
            ),
        )


def get_plan(contract_number: str) -> dict[str, Any] | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM plan WHERE contract_number = ?", (contract_number,)
        ).fetchone()
    if not row:
        return None
    return {
        "fetched_at": row["fetched_at"],
        "product_name": row["product_name"],
        "supply_charge_aud_per_day": row["supply_charge_aud_per_day"],
        "unit_rates": json.loads(row["unit_rates_json"]),
    }


def save_bill_period(contract_number: str, bp: Any) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO bill_period(contract_number, fetched_at, start_date, "
            "end_date, consumption_kwh, cost_label, projection_label) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(contract_number) DO UPDATE SET "
            "fetched_at=excluded.fetched_at, start_date=excluded.start_date, "
            "end_date=excluded.end_date, consumption_kwh=excluded.consumption_kwh, "
            "cost_label=excluded.cost_label, projection_label=excluded.projection_label",
            (
                contract_number,
                datetime.now(UTC).isoformat(),
                bp.start.isoformat(),
                bp.end.isoformat(),
                bp.consumption_kwh,
                bp.cost_label,
                bp.projection_label,
            ),
        )


def get_bill_period(contract_number: str) -> dict[str, Any] | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM bill_period WHERE contract_number = ?", (contract_number,)
        ).fetchone()
    if not row:
        return None
    return {
        "fetched_at": row["fetched_at"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "consumption_kwh": row["consumption_kwh"],
        "cost_label": row["cost_label"],
        "projection_label": row["projection_label"],
    }
