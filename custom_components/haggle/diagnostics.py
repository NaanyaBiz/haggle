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

``schema_version`` is a parsing contract for the automated triage routine —
bump it when the shape changes and update docs/diagnostics.md to match.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
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
    DOMAIN,
    STAT_CONSUMPTION,
    STAT_COST,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import HaggleConfigEntry

DIAGNOSTICS_SCHEMA_VERSION = 1

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

        # Per-series resume state — lets triage spot stalled backfills,
        # missing ToU bands, and never-started solar series without log
        # digging.
        stat_ids = [
            f"{DOMAIN}:{STAT_CONSUMPTION}_{contract}",
            f"{DOMAIN}:{STAT_COST}_{contract}",
        ]
        for band in sorted(coordinator._active_tou_bands):
            stat_ids.extend(coordinator._tariff_stat_ids(band))
        if coordinator._has_solar:
            stat_ids.extend(coordinator._generation_stat_ids())

        for stat_id in stat_ids:
            last_sum, last_date = await coordinator._get_last_stat(stat_id)
            statistics[stat_id] = {
                "last_date": last_date.isoformat() if last_date else None,
                "last_sum": round(last_sum, 3) if last_sum is not None else None,
            }

        update_interval = coordinator.update_interval
        coordinator_block = {
            "last_update_success": coordinator.last_update_success,
            "update_interval_hours": (
                update_interval.total_seconds() / 3600 if update_interval else None
            ),
            "has_solar": coordinator._has_solar,
            "active_tou_bands": sorted(coordinator._active_tou_bands),
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
