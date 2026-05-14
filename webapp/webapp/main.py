"""FastAPI entrypoint for the haggle webapp."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import _bootstrap  # noqa: F401
from . import analytics, setup_flow, storage
from .poller import Poller

STATIC_DIR = Path(__file__).parent / "static"

poller = Poller()


@dataclass
class _SetupSession:
    """Server-side state for an in-flight PKCE wizard.

    Single-user app — one session at a time. Token-gated so a casual
    visitor can't hijack the wizard from another tab.
    """

    token: str
    verifier: str
    state: str
    authorize_url: str
    access_token: str = ""
    refresh_token: str = ""
    auth_pin: str = ""
    bff_pin: str = ""
    contracts: list[Any] = field(default_factory=list)


_setup: _SetupSession | None = None


def _require_setup(token: str) -> _SetupSession:
    if _setup is None or _setup.token != token:
        raise HTTPException(
            status_code=400,
            detail="Setup session expired. Click 'Start setup' again.",
        )
    return _setup


@asynccontextmanager
async def lifespan(_app: FastAPI):
    storage.init_db()
    await poller.start()
    try:
        yield
    finally:
        await poller.stop()


app = FastAPI(title="Haggle — AGL dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _active_contract() -> str:
    c = storage.get_config("contract_number")
    if not c:
        raise HTTPException(
            status_code=503,
            detail="No active contract. Run `python -m webapp.auth` first.",
        )
    return c


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status() -> dict[str, Any]:
    cfg = storage.all_config()
    contract = cfg.get("contract_number")
    return {
        "configured": bool(contract),
        "contract_number": contract,
        "account_number": cfg.get("account_number"),
        "pin_auth": (cfg.get("pin_auth") or "")[:16] + "…" if cfg.get("pin_auth") else "",
        "pin_bff": (cfg.get("pin_bff") or "")[:16] + "…" if cfg.get("pin_bff") else "",
        "last_run_at": poller.last_run_at.isoformat() if poller.last_run_at else None,
        "last_run_ok": poller.last_run_ok,
        "last_error": poller.last_error,
        "earliest_interval": _iso_or_none(
            storage.earliest_interval_date(contract) if contract else None
        ),
        "latest_interval": _iso_or_none(
            storage.latest_interval_date(contract) if contract else None
        ),
        "data_lag_days": analytics.DATA_LAG_DAYS,
        "backfill": poller.backfill,
    }


@app.get("/api/contracts")
def contracts() -> list[dict[str, Any]]:
    return [
        {
            "contract_number": c.contract_number,
            "account_number": c.account_number,
            "address": c.address,
            "fuel_type": c.fuel_type,
            "status": c.status,
            "has_solar": c.has_solar,
        }
        for c in storage.list_contracts()
    ]


@app.get("/api/intervals")
def intervals(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    contract = _active_contract()
    today = datetime.now(UTC).date()
    start = _parse_date(from_date) or (today - timedelta(days=2))
    end = _parse_date(to_date) or today
    start_utc = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    end_utc = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    rows = storage.fetch_intervals(contract, start_utc, end_utc)
    return {"contract": contract, "from": start.isoformat(), "to": end.isoformat(), "rows": rows}


@app.get("/api/daily")
def daily(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    contract = _active_contract()
    today = datetime.now(UTC).date()
    end = _parse_date(to_date) or today
    start = _parse_date(from_date) or (end - timedelta(days=29))
    rows = storage.daily_totals(contract, start, end, analytics.TZ_OFFSET_MINUTES)
    return {"contract": contract, "from": start.isoformat(), "to": end.isoformat(), "rows": rows}


@app.get("/api/bill")
def bill() -> dict[str, Any]:
    contract = _active_contract()
    bp_snapshot = storage.get_bill_period(contract)
    proj = analytics.bill_projection(contract)
    proj_dict = _projection_dict(proj) if proj else None
    if proj_dict is not None:
        # Solar-card visibility uses three signals so it doesn't flicker off
        # when the current bill period happens to have no export yet:
        #   1. AGL flagged the contract has_solar in /v3/overview
        #   2. We've ever observed a non-zero export interval in storage
        #   3. There's export inside the current bill period
        contract_has_solar = any(
            c.has_solar for c in storage.list_contracts()
            if c.contract_number == contract
        )
        proj_dict["has_solar"] = bool(
            contract_has_solar
            or storage.has_any_export(contract)
            or proj_dict["has_solar"]
        )
    return {
        "contract": contract,
        "snapshot": bp_snapshot,
        "projection": proj_dict,
    }


@app.get("/api/plan")
def plan() -> dict[str, Any]:
    contract = _active_contract()
    p = storage.get_plan(contract)
    if not p:
        raise HTTPException(status_code=404, detail="Plan not yet fetched.")
    return {"contract": contract, **p}


@app.get("/api/comparisons")
def comparisons() -> dict[str, Any]:
    contract = _active_contract()
    return {"contract": contract, **analytics.comparisons(contract)}


@app.get("/api/heatmap")
def heatmap(weeks: int = Query(default=8, ge=1, le=52)) -> dict[str, Any]:
    contract = _active_contract()
    return {"contract": contract, **analytics.heatmap(contract, weeks)}


@app.get("/api/raw/hourly")
async def raw_hourly(day: str = Query(...)) -> dict[str, Any]:
    """Debug — return AGL's raw /Hourly JSON for one day.

    Useful for inspecting unfamiliar fields (e.g. solar generation/export).
    Hits AGL live; rate-limited by the same throttle as the poller. The
    poller's long-lived AglClient is reused so token refresh + TLS pin checks
    apply normally.
    """
    contract = _active_contract()
    client = poller._client  # noqa: SLF001 — single-process app
    if client is None:
        raise HTTPException(status_code=503, detail="Poller not running yet.")
    d = _parse_date(day)
    if d is None:
        raise HTTPException(status_code=400, detail="?day=YYYY-MM-DD required")
    bp = storage.get_bill_period(contract)
    bill_start = (
        date.fromisoformat(bp["start_date"]) if bp and bp.get("start_date") else None
    )
    # Hit /Current/Hourly inside current bill, /Previous/Hourly otherwise.
    # Both methods parse to dataclasses, so we issue a raw GET via the client's
    # internal _get to keep the JSON intact.
    from custom_components.haggle.const import AGL_SCALING

    period = f"{d}_{d}"
    base = client.BASE_URL
    if bill_start is not None and d < bill_start:
        url = f"{base}/api/v2/usage/smart/Electricity/{contract}/Previous/Hourly?period={period}&scaling={AGL_SCALING}"
    else:
        url = f"{base}/api/v2/usage/smart/Electricity/{contract}/Current/Hourly?period={period}&scaling={AGL_SCALING}"
    raw = await client._get(url)  # noqa: SLF001
    return {"day": day, "url": url, "raw": raw}


@app.post("/api/backfill")
async def trigger_backfill(
    days: int = Query(default=30, ge=1, le=60),
) -> dict[str, Any]:
    """Re-fetch the last N days from AGL and overwrite stored intervals.

    Uses the live poller session — no re-auth. Runs in the background; poll
    `/api/status.backfill` to follow progress. Must be async so the underlying
    `asyncio.create_task` runs on the event loop, not a thread-pool worker.
    """
    contract = _active_contract()
    started = poller.start_backfill(contract, days)
    if not started:
        raise HTTPException(
            status_code=409,
            detail="Poller not ready or backfill already in progress.",
        )
    return {"started": True, "days": days, "backfill": poller.backfill}


@app.post("/api/refresh")
async def refresh() -> dict[str, Any]:
    await poller.trigger()
    return {
        "ok": poller.last_run_ok,
        "ran_at": poller.last_run_at.isoformat() if poller.last_run_at else None,
        "error": poller.last_error,
    }


# ---------------------------------------------------------------------------
# Setup wizard (in-app PKCE)
# ---------------------------------------------------------------------------


class _ExchangeBody(BaseModel):
    token: str
    callback_url: str


class _SelectBody(BaseModel):
    token: str
    contract_number: str


@app.post("/api/setup/start")
def setup_start() -> dict[str, Any]:
    """Begin a PKCE wizard. Returns the /authorize URL + opaque session token."""
    global _setup
    verifier, challenge = setup_flow.gen_pkce()
    state = secrets.token_urlsafe(16)
    _setup = _SetupSession(
        token=secrets.token_urlsafe(24),
        verifier=verifier,
        state=state,
        authorize_url=setup_flow.build_authorize_url(challenge, state),
    )
    return {"token": _setup.token, "authorize_url": _setup.authorize_url}


@app.post("/api/setup/exchange")
async def setup_exchange(body: _ExchangeBody) -> dict[str, Any]:
    """Trade the pasted callback URL for tokens + contract list."""
    sess = _require_setup(body.token)
    try:
        code = setup_flow.extract_code(body.callback_url.strip(), sess.state)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    try:
        access_token, refresh_token, auth_pin = await setup_flow.exchange_code(
            code, sess.verifier
        )
        contracts, bff_pin = await setup_flow.fetch_contracts(access_token)
    except RuntimeError as err:
        raise HTTPException(status_code=502, detail=str(err))

    sess.access_token = access_token
    sess.refresh_token = refresh_token
    sess.auth_pin = auth_pin
    sess.bff_pin = bff_pin
    sess.contracts = contracts
    return {
        "auth_pin": auth_pin,
        "bff_pin": bff_pin,
        "contracts": [
            {
                "contract_number": c.contract_number,
                "account_number": c.account_number,
                "address": c.address,
                "fuel_type": c.fuel_type,
                "status": c.status,
            }
            for c in contracts
        ],
    }


@app.post("/api/setup/select")
async def setup_select(body: _SelectBody) -> dict[str, Any]:
    """Persist the chosen contract + tokens, then (re)start the poller."""
    global _setup
    sess = _require_setup(body.token)
    if not sess.refresh_token or not sess.contracts:
        raise HTTPException(status_code=400, detail="Run /api/setup/exchange first.")

    chosen = next(
        (c for c in sess.contracts if c.contract_number == body.contract_number),
        None,
    )
    if chosen is None:
        raise HTTPException(status_code=400, detail="Unknown contract_number.")

    storage.upsert_contracts(sess.contracts)
    storage.set_config("refresh_token", sess.refresh_token)
    storage.set_config("contract_number", chosen.contract_number)
    storage.set_config("account_number", chosen.account_number)
    storage.set_config("pin_auth", sess.auth_pin)
    storage.set_config("pin_bff", sess.bff_pin)

    _setup = None  # one-shot; tokens are persisted, session is no longer needed.

    await poller.stop()
    await poller.start()
    return {
        "ok": True,
        "contract_number": chosen.contract_number,
        "address": chosen.address,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date: {raw!r}")


def _iso_or_none(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _projection_dict(p: analytics.BillProjection) -> dict[str, Any]:
    return {
        "period_start": p.period_start.isoformat(),
        "period_end": p.period_end.isoformat(),
        "days_elapsed": p.days_elapsed,
        "days_total": p.days_total,
        "consumption_kwh_to_date": round(p.consumption_kwh_to_date, 3),
        "cost_aud_to_date": round(p.cost_aud_to_date, 2),
        "avg_daily_kwh": round(p.avg_daily_kwh, 3),
        "avg_daily_cost_aud": round(p.avg_daily_cost_aud, 2),
        "projected_kwh": round(p.projected_kwh, 1),
        "projected_cost_aud": round(p.projected_cost_aud, 2),
        "supply_charge_total_aud": round(p.supply_charge_total_aud, 2),
        "export_kwh_to_date": round(p.export_kwh_to_date, 3),
        "credit_aud_to_date": round(p.credit_aud_to_date, 2),
        "avg_daily_export_kwh": round(p.avg_daily_export_kwh, 3),
        "projected_export_kwh": round(p.projected_export_kwh, 1),
        "projected_credit_aud": round(p.projected_credit_aud, 2),
        "net_cost_to_date": round(p.net_cost_to_date, 2),
        "projected_net_cost_aud": round(p.projected_net_cost_aud, 2),
        "self_consumption_ratio": p.self_consumption_ratio,
        "has_solar": p.export_kwh_to_date > 0,
        "agl_label_cost": p.agl_label_cost,
        "agl_label_projection": p.agl_label_projection,
    }


@app.exception_handler(HTTPException)
async def _http_err(_req: Any, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
