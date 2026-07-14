"""Diagnostics support for haggle.

Produces the JSON behind the integration card's "Download diagnostics"
button. The file is designed to be attached to public GitHub issues, so
EVERY field must survive that assumption:

- The Auth0 refresh token is redacted outright.
- Account and contract numbers are replaced by stable anonymous references
  (``anon-<hmac prefix>``, keyed with the HA install's instance id) — the
  same install always produces the same reference, so repeat reports
  correlate without exposing the number, and low-entropy AGL identifiers
  cannot be brute-forced offline.
- SPKI pin hashes are collapsed to booleans.
- A final scrub pass string-replaces any residual occurrence of the raw
  identifiers (they hide inside statistic IDs, display names, and the entry
  unique_id), so a future field addition cannot leak them by accident.

``schema_version`` is a parsing contract for any tooling that reads these
files — bump it when the shape changes and update docs/diagnostics.md to
match.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.helpers import instance_id
from homeassistant.loader import async_get_integration

from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_PINNED_SPKI_AUTH,
    CONF_PINNED_SPKI_BFF,
    CONF_REFRESH_TOKEN,
    CONF_SOLAR_HEAL,
    CONF_SOLAR_STALL_SPANS,
    DOMAIN,
    STAT_CONSUMPTION,
    STAT_COST,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import HaggleConfigEntry

DIAGNOSTICS_SCHEMA_VERSION = 2

_TO_REDACT = {CONF_REFRESH_TOKEN}


def _anon_ref(value: str, key: str) -> str:
    """Stable anonymous reference for an account/contract identifier.

    HMAC keyed with the Home Assistant per-install instance id — a bare
    sha256 would be brute-forceable (AGL identifiers are 10-digit numerics,
    ~2^33 candidates, and a 10-hex prefix matches uniquely). The key never
    leaves the install, so the reference stays stable per install for
    correlating repeat reports but cannot be enumerated offline.
    """
    digest = hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()
    return "anon-" + digest[:10]


def _round_floats(obj: Any) -> Any:
    """Round every float to 3 dp — usage data doesn't need more precision."""
    if isinstance(obj, float):
        return round(obj, 3)
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_round_floats(v) for v in obj]
    return obj


def _scrub(obj: Any, replacements: dict[str, str]) -> Any:
    """Recursively replace raw identifiers in every string, keys included."""
    if isinstance(obj, str):
        for raw, ref in replacements.items():
            if raw:
                obj = obj.replace(raw, ref)
        return obj
    if isinstance(obj, dict):
        return {
            _scrub(k, replacements): _scrub(v, replacements) for k, v in obj.items()
        }
    if isinstance(obj, list | tuple):
        return [_scrub(v, replacements) for v in obj]
    return obj


async def _series_coverage(
    hass: HomeAssistant, stat_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """First/last stored hour, row count, and last sum for each series.

    One batched recorder query over the full history. ``first_date`` is the
    field that makes a leading hole visible (#128: a healthy ``last_date``
    while early billing-period days are missing entirely); ``row_count``
    against the first→last span exposes interior gaps. Degrades to
    all-``None`` entries if the recorder query fails — diagnostics must never
    raise.
    """
    from homeassistant.components.recorder.statistics import (
        statistics_during_period,
    )
    from homeassistant.helpers.recorder import get_instance

    empty: dict[str, Any] = {
        "first_date": None,
        "last_date": None,
        "row_count": 0,
        "last_sum": None,
    }
    try:
        result = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            datetime(2000, 1, 1, tzinfo=UTC),  # long before any AGL data
            datetime.now(UTC),
            set(stat_ids),
            "hour",
            None,
            {"start", "sum"},
        )
    except Exception:
        return {stat_id: dict(empty) for stat_id in stat_ids}

    def _row_date(row: Any) -> str | None:
        start = row.get("start")
        if start is None:
            return None
        return datetime.fromtimestamp(float(start), tz=UTC).date().isoformat()

    coverage: dict[str, dict[str, Any]] = {}
    for stat_id in stat_ids:
        rows = result.get(stat_id) or []
        entry = dict(empty)
        if rows:
            last_sum = rows[-1].get("sum")
            entry = {
                "first_date": _row_date(rows[0]),
                "last_date": _row_date(rows[-1]),
                "row_count": len(rows),
                "last_sum": round(last_sum, 3) if last_sum is not None else None,
            }
        coverage[stat_id] = entry
    return coverage


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HaggleConfigEntry
) -> dict[str, Any]:
    """Return anonymized diagnostics for a config entry.

    Works even when setup failed (expired token, AGL outage): runtime_data is
    unset in that case — exactly when diagnostics matter most — so the
    coordinator/statistics blocks degrade to None/{} instead of raising.
    """
    # Never-published, random, stable per HA install → HMAC key.
    anon_key = await instance_id.async_get(hass)
    contract: str = entry.data.get(CONF_CONTRACT_NUMBER, "")
    account: str = entry.data.get(CONF_ACCOUNT_NUMBER, "")
    contract_ref = _anon_ref(contract, anon_key) if contract else None
    account_ref = _anon_ref(account, anon_key) if account else None

    integration = await async_get_integration(hass, DOMAIN)

    entry_data = dict(async_redact_data(dict(entry.data), _TO_REDACT))
    # Pins are public cert material but still fingerprint an install's
    # capture history — presence booleans carry the diagnostic signal.
    pin_auth = bool(entry_data.pop(CONF_PINNED_SPKI_AUTH, ""))
    pin_bff = bool(entry_data.pop(CONF_PINNED_SPKI_BFF, ""))
    # One-time solar heal record ({state, floor, attempts}) — first-class
    # field: distinguishes "period sensors deliberately blank while a heal
    # runs" from "gate failed" from "heal gave up" (#128 beta.3 round).
    solar_heal = entry_data.pop(CONF_SOLAR_HEAL, None)
    # Stall give-up spans — the marker rows written at give-up make the
    # coverage stats look healthy over the hole, so this is the only place
    # the hole is visible (CO-16.4). None = never gave up.
    stall_spans = entry_data.pop(CONF_SOLAR_STALL_SPANS, None)

    runtime = getattr(entry, "runtime_data", None)
    coordinator = runtime.coordinator if runtime is not None else None

    coordinator_block: dict[str, Any] | None = None
    statistics: dict[str, Any] = {}
    if coordinator is not None:
        data_block: dict[str, Any] | None = None
        if coordinator.data is not None:
            data_block = dataclasses.asdict(coordinator.data)
            # frozenset is not JSON-serializable.
            data_block["active_tariffs"] = sorted(data_block["active_tariffs"])

        # Per-series coverage — spots stalled backfills, missing ToU bands,
        # never-started solar series, AND leading holes (#128), without log
        # digging.
        stat_ids = [
            f"{DOMAIN}:{STAT_CONSUMPTION}_{contract}",
            f"{DOMAIN}:{STAT_COST}_{contract}",
        ]
        for band in sorted(coordinator._active_tou_bands):
            stat_ids.extend(coordinator._tariff_stat_ids(band))
        if coordinator._has_solar:
            stat_ids.extend(coordinator._generation_stat_ids())
        statistics = await _series_coverage(hass, stat_ids)

        update_interval = coordinator.update_interval
        # str(last_exception) is safe to publish: exception messages are
        # body-scrubbed at raise time (AGENTS.md — raw AGL/Auth0 bodies never
        # reach exception text) and the identifier scrub below applies on top.
        last_exception = coordinator.last_exception
        coordinator_block = {
            "last_update_success": coordinator.last_update_success,
            "last_exception": str(last_exception) if last_exception else None,
            "update_interval_hours": (
                update_interval.total_seconds() / 3600 if update_interval else None
            ),
            "has_solar": coordinator._has_solar,
            "active_tou_bands": sorted(coordinator._active_tou_bands),
            "bill_period_start": (
                coordinator.last_bill_start.isoformat()
                if coordinator.last_bill_start
                else None
            ),
            "data": _round_floats(data_block),
        }

    result: dict[str, Any] = {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "integration": {"domain": DOMAIN, "version": str(integration.version)},
        # False → setup failed before runtime data existed; the coordinator
        # and statistics blocks are absent and the failure itself is the bug.
        "runtime_available": coordinator is not None,
        "contract_ref": contract_ref,
        "account_ref": account_ref,
        "timezone": hass.config.time_zone,
        "entry": {
            "data": entry_data,
            "unique_id": entry.unique_id,
            "pin_present_auth": pin_auth,
            "pin_present_bff": pin_bff,
        },
        "solar_heal": solar_heal,
        "stall_give_up_spans": stall_spans,
        "coordinator": coordinator_block,
        "statistics": statistics,
    }

    replacements: dict[str, str] = {}
    if contract and contract_ref:
        replacements[contract] = contract_ref
    if account and account_ref:
        replacements[account] = account_ref
    scrubbed: dict[str, Any] = _scrub(result, replacements)
    return scrubbed
