"""Solar-aware parser for AGL /Hourly responses.

Lives in the webapp (not custom_components/haggle/agl/parser.py) so changes
here don't risk the HA integration's parser tests. Mirrors the safety rules
from that parser:

  - kWh from consumption.quantity (outer) — never values.quantity (inner)
  - dateTime is slot-start UTC
  - filter type='none' (future/unavailable)
  - filter all-zero placeholders (AEMO feed not yet delivered)

Solar/export is read from a sibling `generation` key on each item:

    items[].generation.quantity   — kWh exported in the slot
    items[].generation.amount     — AUD credit (FiT) for the slot
    items[].generation.type       — peak|offpeak|shoulder|normal|none

If your AGL response uses a different key (e.g. `solar`, `export`,
`feedIn`), edit GENERATION_KEYS below — they're tried in order, first
non-empty wins.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

GENERATION_KEYS: tuple[str, ...] = ("generation", "solar", "export", "feedIn")


def _safe_float(raw: Any) -> float:
    try:
        value = float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value < 0:
        _LOGGER.warning("Rejecting non-finite/negative value: %r", raw)
        return 0.0
    return value


def _pick_generation(item: dict[str, Any]) -> dict[str, Any]:
    for key in GENERATION_KEYS:
        block = item.get(key)
        if isinstance(block, dict) and block:
            return block
    return {}


def parse_intervals_with_solar(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of dicts, one per 30-min slot.

    Keys: dt (datetime UTC), kwh, cost_aud, kwh_export, credit_aud, rate_type.
    Items at the same dateTime are merged so that responses which split
    consumption + generation into separate items still reconcile correctly.
    """
    bucket: dict[datetime, dict[str, Any]] = {}

    for section in data.get("sections") or []:
        for item in section.get("items") or []:
            dt_str: str = item.get("dateTime", "")
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
            except (ValueError, AttributeError):
                continue

            cons = item.get("consumption") or {}
            gen = _pick_generation(item)

            cons_type = cons.get("type")
            gen_type = gen.get("type")
            rate_type = (
                cons_type
                if cons_type and cons_type != "none"
                else (gen_type if gen_type and gen_type != "none" else "none")
            )
            if rate_type == "none":
                continue

            row = bucket.setdefault(
                dt,
                {
                    "dt": dt,
                    "kwh": 0.0,
                    "cost_aud": 0.0,
                    "kwh_export": 0.0,
                    "credit_aud": 0.0,
                    "rate_type": rate_type,
                },
            )
            if cons:
                row["kwh"] += _safe_float(cons.get("quantity"))
                row["cost_aud"] += _safe_float(cons.get("amount"))
            if gen:
                row["kwh_export"] += _safe_float(gen.get("quantity"))
                row["credit_aud"] += _safe_float(gen.get("amount"))

    return [
        r
        for r in bucket.values()
        if not (
            r["kwh"] == 0.0
            and r["cost_aud"] == 0.0
            and r["kwh_export"] == 0.0
            and r["credit_aud"] == 0.0
        )
    ]
