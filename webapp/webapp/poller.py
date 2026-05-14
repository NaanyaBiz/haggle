"""Background AGL poller.

Runs as an asyncio task inside the FastAPI process. Mirrors the cadence and
backfill/rewindow logic from custom_components/haggle/coordinator.py:

  - Daily 24h cycle: fetch any missing days back to BACKFILL_DAYS, then re-fetch
    the trailing REWINDOW_DAYS (self-heals AGL's day-late AEMO backfills).
  - Plan + bill_period refreshed every 6h.
  - Endpoint selection per day: Current/Hourly inside current bill period,
    Previous/Hourly before it.
  - Backfill is throttled BACKFILL_INTER_REQUEST_DELAY between days, and the
    chunk caps at BACKFILL_CHUNK_DAYS per cycle so the BFF doesn't 429.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta

import aiohttp

from . import _bootstrap  # noqa: F401
from . import storage
from custom_components.haggle.agl.client import (  # noqa: E402
    AGLAuthError,
    AGLError,
    AGLRateLimitError,
    AglAuth,
    AglClient,
)
from custom_components.haggle.agl.pinning import (  # noqa: E402
    AGL_AUTH_HOST_NAME,
    AGL_BFF_HOST_NAME,
    HagglePinningConnector,
)
from custom_components.haggle.const import (  # noqa: E402
    AGL_SCALING,
    BACKFILL_CHUNK_DAYS,
    BACKFILL_DAYS,
    BACKFILL_INTER_REQUEST_DELAY,
    REWINDOW_DAYS,
)
from .parser_solar import parse_intervals_with_solar

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 24 * 60 * 60
PLAN_REFRESH_SECONDS = 6 * 60 * 60


class Poller:
    """Owns the long-lived aiohttp session + AGL client and the loop task."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._client: AglClient | None = None
        self._auth: AglAuth | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_plan_fetch: datetime | None = None
        self.last_run_at: datetime | None = None
        self.last_run_ok: bool | None = None
        self.last_error: str | None = None
        # On-demand backfill state (POST /api/backfill).
        self._backfill_task: asyncio.Task[None] | None = None
        self.backfill: dict[str, object] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        cfg = storage.all_config()
        refresh_token = cfg.get("refresh_token")
        contract_number = cfg.get("contract_number")
        if not refresh_token or not contract_number:
            _LOGGER.warning(
                "Poller not started — refresh_token/contract_number missing. "
                "Run `python -m webapp.auth` first."
            )
            return

        pin_auth = cfg.get("pin_auth", "")
        pin_bff = cfg.get("pin_bff", "")
        connector = HagglePinningConnector(on_new_connection=_pin_validator(pin_auth, pin_bff))
        self._session = aiohttp.ClientSession(connector=connector)

        async def _persist(new_token: str) -> None:
            storage.set_config("refresh_token", new_token)

        self._auth = AglAuth(refresh_token, _persist)
        self._client = AglClient(self._auth, self._session)
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(contract_number), name="agl-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._session:
            await self._session.close()
        self._session = None
        self._client = None
        self._auth = None

    async def trigger(self) -> None:
        """Force an immediate poll cycle (used by /api/refresh)."""
        cfg = storage.all_config()
        contract_number = cfg.get("contract_number")
        if not contract_number or self._client is None:
            return
        await self._cycle(contract_number)

    def start_backfill(self, contract_number: str, days: int) -> bool:
        """Schedule a deep backfill in the background. Returns False if busy.

        Reuses the live AglClient so no re-auth is needed. Each fetched day
        is upserted via storage so existing rows get their solar fields
        populated in place.
        """
        if self._client is None:
            return False
        if (
            self._backfill_task is not None
            and not self._backfill_task.done()
        ):
            return False
        days = max(1, min(days, 60))  # AGL /Hourly only goes back ~30-60 days
        self._backfill_task = asyncio.create_task(
            self._run_backfill(contract_number, days),
            name="agl-backfill",
        )
        return True

    async def _run_backfill(self, contract_number: str, days: int) -> None:
        from . import analytics  # local import — avoid module-import cycle

        bp_snapshot = storage.get_bill_period(contract_number)
        bill_start = (
            date.fromisoformat(bp_snapshot["start_date"])
            if bp_snapshot and bp_snapshot.get("start_date")
            else None
        )
        today = datetime.now(UTC).date()
        end = today - timedelta(days=analytics.DATA_LAG_DAYS)
        start = today - timedelta(days=days)
        if start > end:
            return

        self.backfill = {
            "state": "running",
            "from": start.isoformat(),
            "to": end.isoformat(),
            "days_total": (end - start).days + 1,
            "days_done": 0,
            "rate_limited_pauses": 0,
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "error": None,
        }
        _LOGGER.info("Backfill start: %s..%s (%s days)", start, end, days)

        try:
            current = start
            first = True
            while current <= end:
                if not first:
                    await asyncio.sleep(BACKFILL_INTER_REQUEST_DELAY)
                first = False
                try:
                    use_previous = bill_start is not None and current < bill_start
                    rows = await self._fetch_day_with_solar(
                        contract_number, current, use_previous
                    )
                    storage.upsert_intervals(contract_number, rows)
                except AGLRateLimitError as err:
                    _LOGGER.warning("Backfill 429 at %s; pausing 30s: %s", current, err)
                    self.backfill["rate_limited_pauses"] = (
                        int(self.backfill["rate_limited_pauses"]) + 1
                    )
                    await asyncio.sleep(30)
                    continue  # retry same day
                except AGLError as err:
                    _LOGGER.debug("Backfill skip %s: %s", current, err)
                current += timedelta(days=1)
                self.backfill["days_done"] = int(self.backfill["days_done"]) + 1
            self.backfill["state"] = "done"
        except AGLAuthError as err:
            self.backfill["state"] = "error"
            self.backfill["error"] = f"auth: {err}"
            _LOGGER.error("Backfill auth failed: %s", err)
        except Exception as err:  # noqa: BLE001
            self.backfill["state"] = "error"
            self.backfill["error"] = repr(err)
            _LOGGER.exception("Backfill crashed")
        finally:
            self.backfill["finished_at"] = datetime.now(UTC).isoformat()
            _LOGGER.info("Backfill end: %s", self.backfill)

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self, contract_number: str) -> None:
        # Run an initial cycle right away.
        await self._cycle(contract_number)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return
            await self._cycle(contract_number)

    async def _cycle(self, contract_number: str) -> None:
        assert self._client is not None
        self.last_run_at = datetime.now(UTC)
        try:
            # Plan + bill_period — fast, refresh every PLAN_REFRESH_SECONDS.
            now = datetime.now(UTC)
            need_plan = (
                self._last_plan_fetch is None
                or (now - self._last_plan_fetch).total_seconds() >= PLAN_REFRESH_SECONDS
            )
            if need_plan:
                plan = await self._client.async_get_plan(contract_number)
                storage.save_plan(contract_number, plan)
                bp = await self._client.async_get_usage_summary(contract_number)
                storage.save_bill_period(contract_number, bp)
                self._last_plan_fetch = now
            else:
                bp = None

            bill_start = (
                bp.start
                if bp is not None
                else _bill_start_from_storage(contract_number)
            )

            # Range to fetch this cycle: gap-fill since latest stored row, plus
            # trailing REWINDOW_DAYS to self-heal AGL's day-late backfills.
            today = datetime.now(UTC).date()
            yesterday = today - timedelta(days=1)
            latest = storage.latest_interval_date(contract_number)

            if latest is None:
                fetch_start = today - timedelta(days=BACKFILL_DAYS)
            elif latest < today - timedelta(days=REWINDOW_DAYS):
                fetch_start = latest + timedelta(days=1)
            else:
                fetch_start = today - timedelta(days=REWINDOW_DAYS)

            backfill_floor = today - timedelta(days=BACKFILL_DAYS)
            if fetch_start < backfill_floor:
                fetch_start = backfill_floor

            fetch_end = min(
                yesterday, fetch_start + timedelta(days=BACKFILL_CHUNK_DAYS - 1)
            )

            if fetch_start <= fetch_end:
                await self._fetch_range(contract_number, fetch_start, fetch_end, bill_start)

            self.last_run_ok = True
            self.last_error = None
            _LOGGER.info(
                "Poll OK — fetched %s..%s (latest stored: %s)",
                fetch_start, fetch_end, latest,
            )
        except AGLAuthError as err:
            self.last_run_ok = False
            self.last_error = f"auth: {err}"
            _LOGGER.error("Auth failed — re-run `python -m webapp.auth`: %s", err)
        except AGLError as err:
            self.last_run_ok = False
            self.last_error = str(err)
            _LOGGER.warning("Poll error: %s", err)
        except Exception as err:  # noqa: BLE001
            self.last_run_ok = False
            self.last_error = repr(err)
            _LOGGER.exception("Poll crashed")

    async def _fetch_range(
        self,
        contract_number: str,
        start: date,
        end: date,
        bill_start: date | None,
    ) -> None:
        assert self._client is not None
        current = start
        first = True
        while current <= end:
            if not first:
                await asyncio.sleep(BACKFILL_INTER_REQUEST_DELAY)
            first = False
            try:
                use_previous = bill_start is not None and current < bill_start
                rows = await self._fetch_day_with_solar(
                    contract_number, current, use_previous
                )
                n = storage.upsert_intervals(contract_number, rows)
                _LOGGER.debug("Imported %s intervals for %s", n, current)
            except AGLRateLimitError as err:
                _LOGGER.warning(
                    "Rate-limited at %s; halting chunk: %s", current, err
                )
                break
            except AGLError as err:
                _LOGGER.debug("Skip %s: %s", current, err)
            current += timedelta(days=1)

    async def _fetch_day_with_solar(
        self, contract_number: str, day: date, use_previous: bool
    ) -> list[dict]:
        """Raw /Hourly GET → parser_solar (captures consumption + generation)."""
        assert self._client is not None
        period = f"{day}_{day}"
        seg = "Previous" if use_previous else "Current"
        url = (
            f"{self._client.BASE_URL}/api/v2/usage/smart/Electricity/"
            f"{contract_number}/{seg}/Hourly?period={period}&scaling={AGL_SCALING}"
        )
        raw = await self._client._get(url)  # noqa: SLF001 — raw JSON for solar
        return parse_intervals_with_solar(raw)


def _pin_validator(pin_auth: str, pin_bff: str):
    """Return a callback that warns on SPKI mismatch (warn-only by design)."""
    expected = {AGL_AUTH_HOST_NAME: pin_auth, AGL_BFF_HOST_NAME: pin_bff}

    def _check(host: str, observed: str) -> None:
        want = expected.get(host, "")
        if want and observed != want:
            _LOGGER.warning(
                "TLS pin mismatch for %s: expected %s, observed %s "
                "(re-run `python -m webapp.auth` to re-pin)",
                host, want[:12] + "…", observed[:12] + "…",
            )

    return _check


def _bill_start_from_storage(contract_number: str) -> date | None:
    bp = storage.get_bill_period(contract_number)
    if not bp:
        return None
    try:
        return date.fromisoformat(bp["start_date"])
    except (TypeError, ValueError):
        return None
