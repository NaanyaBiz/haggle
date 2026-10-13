"""User-configured Time-of-Use windows (#141) — pure logic, no HA imports.

AGL's per-interval ``consumption.type`` does not follow the tariff (#126,
#224, #292), so a configured entry derives each slot's band locally from the
slot's corrected (#292) start time in the CONTRACT's zone. This module holds
everything about that derivation that does not touch Home Assistant, so the
grammar, validation, classification and fingerprint are unit-tested
directly (tests/test_tariff_windows.py):

- Text grammar (options form, one field per band):
  ``HH:MM-HH:MM[ weekdays|weekends|all][, ...]`` — half-hour boundaries only,
  ``end < start`` wraps past midnight, ``24:00`` (or ``00:00``) as an end
  means end of day, at most MAX_RANGES_PER_BAND ranges per band.
- ``days`` applies to the slot's OWN contract-local weekday (Mon-Fri =
  weekdays). A wrapping weekday window ``22:00-07:00 weekdays`` therefore
  covers 00:00-07:00 and 22:00-24:00 of every Monday-Friday — never
  Saturday's early hours. Public holidays are ordinary days (documented
  limitation; band cost is AGL's own per-slot amount, so totals are exact).
- Overlaps are rejected per day class: a weekday window and a weekend window
  may share hours; ``all`` overlaps both. Gaps go to the residual band.
- Local wall clock only (zoneinfo, DST-aware). No standard-time option.

The compiled table is ``(weekday_row, weekend_row)``, 48 bands each, so a
classification is O(1) and every slot gets exactly one band — the bands are
a partition of the aggregate by construction.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any

from .const import (
    MAX_RANGES_PER_BAND,
    MAX_RATE_C_PER_KWH,
    MIN_RATE_C_PER_KWH,
    RESIDUAL_BANDS,
    TARIFF_CONFIG_VERSION,
    TARIFF_MODE_FLAT,
    TARIFF_MODE_WINDOWS,
    TARIFF_OFFPEAK,
    TARIFF_PEAK,
    TARIFF_SHOULDER,
    TARIFF_SUPEROFFPEAK,
    WINDOW_BANDS,
)

DAYS_ALL = "all"
DAYS_WEEKDAYS = "weekdays"
DAYS_WEEKENDS = "weekends"
DAY_VALUES = (DAYS_ALL, DAYS_WEEKDAYS, DAYS_WEEKENDS)
# Compiled-table rows.
_WEEKDAY, _WEEKEND = 0, 1
_DAY_ROWS: dict[str, tuple[int, ...]] = {
    DAYS_ALL: (_WEEKDAY, _WEEKEND),
    DAYS_WEEKDAYS: (_WEEKDAY,),
    DAYS_WEEKENDS: (_WEEKEND,),
}
SLOTS_PER_DAY = 48
_DAY_MINUTES = 24 * 60
# Bound on one band's raw text: the parser never sees more than this.
MAX_TEXT_LEN = 200

# Rate sensors: `normal` (Anytime) is covered by the plain `unit_rate`.
RATE_BANDS = (TARIFF_PEAK, TARIFF_OFFPEAK, TARIFF_SHOULDER, TARIFF_SUPEROFFPEAK)
LEGACY_RATE_BANDS = (TARIFF_PEAK, TARIFF_OFFPEAK, TARIFF_SHOULDER)

# Implied-rate profile (diagnostics): slots below this kWh carry too little
# energy for a meaningful c/kWh ratio.
PROFILE_MIN_KWH = 0.005

_RANGE_RE = re.compile(
    r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})(?:\s+([a-z]+))?$", re.IGNORECASE
)

type Table = tuple[tuple[str, ...], tuple[str, ...]]


class WindowError(ValueError):
    """A window/config validation failure, carrying an options-flow error code.

    ``bands`` names the bands involved (an overlap names both) — never the
    raw user text, which must not reach logs or diagnostics.
    """

    def __init__(self, code: str, bands: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.bands = bands


def _minute_of(hours: str, minutes: str) -> int:
    """Minute of day for ``HH:MM`` (24:00 allowed only as an end)."""
    h, m = int(hours), int(minutes)
    if h > 24 or m > 59 or (h == 24 and m != 0):
        raise WindowError("invalid_window_format")
    if m not in (0, 30):
        raise WindowError("window_not_half_hour")
    return h * 60 + m


def _fmt(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _parse_one(part: str, band: str) -> dict[str, str]:
    match = _RANGE_RE.match(part)
    if match is None:
        raise WindowError("invalid_window_format", (band,))
    days = (match.group(5) or DAYS_ALL).lower()
    if days not in DAY_VALUES:
        raise WindowError("invalid_window_format", (band,))
    try:
        start = _minute_of(match.group(1), match.group(2))
        end = _minute_of(match.group(3), match.group(4))
    except WindowError as err:
        raise WindowError(err.code, (band,)) from None
    if start == _DAY_MINUTES:
        raise WindowError("invalid_window_format", (band,))
    if start == end:  # before normalising: "00:00-00:00" is empty too
        raise WindowError("window_empty", (band,))
    if end == 0:
        end = _DAY_MINUTES  # "00:00" as an end means end of day
    return {"band": band, "start": _fmt(start), "end": _fmt(end), "days": days}


def parse_ranges(text: str, band: str) -> list[dict[str, str]]:
    """Parse one band's text into canonical windows (raises WindowError)."""
    if len(text) > MAX_TEXT_LEN:
        raise WindowError("invalid_window_format", (band,))
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) > MAX_RANGES_PER_BAND:
        raise WindowError("too_many_windows", (band,))
    return [_parse_one(part, band) for part in parts]


def render_ranges(windows: Iterable[Mapping[str, str]], band: str) -> str:
    """The canonical text for one band — a fixed point of parse_ranges."""
    return ", ".join(
        f"{w['start']}-{w['end']}" + ("" if w["days"] == DAYS_ALL else f" {w['days']}")
        for w in windows
        if w["band"] == band
    )


def _slot_indices(window: Mapping[str, str]) -> list[int]:
    """Half-hour slot indices a window covers within one local day."""
    start = _minute_of(*window["start"].split(":")) // 30
    end = _minute_of(*window["end"].split(":")) // 30
    if end > start:
        return list(range(start, end))
    return list(range(start, SLOTS_PER_DAY)) + list(range(end))


def check_overlaps(windows: Iterable[Mapping[str, str]]) -> None:
    """Reject two windows claiming one (day class, slot), naming both bands.

    Per day class: a weekday and a weekend window may share hours; ``all``
    claims both rows. A band's own windows may not overlap each other.
    """
    owner: dict[tuple[int, int], str] = {}
    for window in windows:
        for row in _DAY_ROWS[window["days"]]:
            for idx in _slot_indices(window):
                if (row, idx) in owner:
                    raise WindowError(
                        "windows_overlap", (owner[(row, idx)], window["band"])
                    )
                owner[(row, idx)] = window["band"]


def _canonical_windows(raw: object) -> list[dict[str, str]]:
    """Re-validate stored windows through the text grammar, sorted."""
    if not isinstance(raw, list):
        raise WindowError("invalid_config")
    by_band: dict[str, list[str]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise WindowError("invalid_config")
        band, start, end, days = (
            item.get("band"),
            item.get("start"),
            item.get("end"),
            item.get("days", DAYS_ALL),
        )
        if band not in WINDOW_BANDS or not all(
            isinstance(v, str) for v in (start, end, days)
        ):
            raise WindowError("invalid_config")
        by_band.setdefault(str(band), []).append(f"{start}-{end} {days}")
    windows: list[dict[str, str]] = []
    for band in WINDOW_BANDS:
        windows += parse_ranges(", ".join(by_band.get(band, [])), band)
    windows.sort(key=lambda w: (WINDOW_BANDS.index(w["band"]), w["days"], w["start"]))
    return windows


def _canonical_rates(raw: object) -> dict[str, float]:
    """Stored rates (AUD/kWh) bounded to the c/kWh input range."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise WindowError("invalid_config")
    rates: dict[str, float] = {}
    for band, value in raw.items():
        if band not in WINDOW_BANDS or isinstance(value, bool):
            raise WindowError("invalid_config")
        if not isinstance(value, int | float) or not math.isfinite(value):
            raise WindowError("rate_out_of_range", (str(band),))
        cents = value * 100
        if not MIN_RATE_C_PER_KWH - 1e-9 <= cents <= MAX_RATE_C_PER_KWH + 1e-9:
            raise WindowError("rate_out_of_range", (str(band),))
        rates[str(band)] = float(value)
    return dict(sorted(rates.items()))


def _canonical(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WindowError("invalid_config")
    if raw.get("version") != TARIFF_CONFIG_VERSION:
        raise WindowError("unsupported_version")
    mode = raw.get("mode")
    if mode == TARIFF_MODE_FLAT:
        return {"version": TARIFF_CONFIG_VERSION, "mode": TARIFF_MODE_FLAT}
    if mode != TARIFF_MODE_WINDOWS:
        raise WindowError("invalid_config")
    residual = raw.get("residual")
    if residual not in RESIDUAL_BANDS:
        raise WindowError("invalid_config")
    windows = _canonical_windows(raw.get("windows"))
    if not windows:
        raise WindowError("no_windows")
    check_overlaps(windows)
    return {
        "version": TARIFF_CONFIG_VERSION,
        "mode": TARIFF_MODE_WINDOWS,
        "residual": residual,
        "windows": windows,
        "rates": _canonical_rates(raw.get("rates")),
    }


def validate(raw: object) -> tuple[dict[str, Any] | None, str | None]:
    """``(canonical, None)`` for a valid stored config, else ``(None, code)``.

    Every read of entry.options goes through here, so a hand-edited or
    newer-build value (unknown ``version``, unknown field value) fails SAFE
    as ``invalid`` rather than being half-understood. The code is an enum
    string — never the offending text.
    """
    try:
        return _canonical(raw), None
    except WindowError as err:
        return None, err.code
    except TypeError, ValueError:
        return None, "invalid_config"


def build_config(
    mode: str,
    residual: str,
    texts: Mapping[str, str],
    rates_c: Mapping[str, float | None],
) -> tuple[dict[str, Any] | None, dict[str, str], tuple[str, ...]]:
    """Options-form input → ``(canonical, errors, overlap_bands)``.

    ``errors`` maps a form field (a band key, or ``base``) to an error code;
    ``overlap_bands`` names the two bands of a ``windows_overlap`` error for
    the form's description placeholders.
    """
    if mode == TARIFF_MODE_FLAT:
        return {"version": TARIFF_CONFIG_VERSION, "mode": TARIFF_MODE_FLAT}, {}, ()
    errors: dict[str, str] = {}
    windows: list[dict[str, str]] = []
    for band in WINDOW_BANDS:
        try:
            windows += parse_ranges(texts.get(band) or "", band)
        except WindowError as err:
            errors[band] = err.code
    rates: dict[str, float] = {}
    for band, cents in rates_c.items():
        if cents is None:
            continue
        if not MIN_RATE_C_PER_KWH <= cents <= MAX_RATE_C_PER_KWH:
            errors[f"{band}_rate"] = "rate_out_of_range"
            continue
        rates[band] = round(cents / 100.0, 6)
    if errors:
        return None, errors, ()
    raw = {
        "version": TARIFF_CONFIG_VERSION,
        "mode": TARIFF_MODE_WINDOWS,
        "residual": residual,
        "windows": windows,
        "rates": rates,
    }
    try:
        return _canonical(raw), {}, ()
    except WindowError as err:
        return None, {"base": err.code}, err.bands


def compile_table(config: Mapping[str, Any]) -> Table:
    """Valid canonical windows config → (weekday row, weekend row)."""
    rows = [[str(config["residual"])] * SLOTS_PER_DAY for _ in range(2)]
    for window in config["windows"]:
        for row in _DAY_ROWS[window["days"]]:
            for idx in _slot_indices(window):
                rows[row][idx] = window["band"]
    return tuple(rows[_WEEKDAY]), tuple(rows[_WEEKEND])


def slot_index(local: datetime) -> int:
    """Half-hour index of a contract-local wall-clock instant."""
    return local.hour * 2 + local.minute // 30


def band_of(dt_utc: datetime, tz: tzinfo, table: Table) -> str:
    """The band of the slot starting at ``dt_utc`` (contract-local clock).

    On a fall-back day both 02:xx folds share the wall-clock label and so
    the band; on a spring-forward day the missing hour simply has no slots.
    """
    local = dt_utc.astimezone(UTC).astimezone(tz)
    row = _WEEKEND if local.weekday() >= 5 else _WEEKDAY
    return table[row][slot_index(local)]


def configured_bands(config: Mapping[str, Any] | None) -> frozenset[str]:
    """Bands a valid config can produce (window bands + a used residual)."""
    if config is None or config.get("mode") != TARIFF_MODE_WINDOWS:
        return frozenset()
    weekday, weekend = compile_table(config)
    return frozenset(weekday) | frozenset(weekend)


def fingerprint(config: Mapping[str, Any]) -> str:
    """Stable id of the ATTRIBUTION settings — rates excluded, so changing a
    rate never records a new attribution basis."""
    basis = {
        "mode": config.get("mode"),
        "residual": config.get("residual"),
        "windows": config.get("windows", []),
    }
    blob = json.dumps(basis, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def desired_rate_bands(
    config: Mapping[str, Any] | None, active_tou_bands: Iterable[str]
) -> frozenset[str]:
    """The per-band rate sensors an entry should register (M4).

    One function for sensor.py (what to register) and the coordinator (when
    to reload): legacy (``config`` None — unconfigured or invalid) keeps
    v0.5.0's set, the active type-derived bands among peak/offpeak/shoulder;
    a flat config registers none; a windows config registers its configured
    rate bands.
    """
    if config is None:
        return frozenset(active_tou_bands) & frozenset(LEGACY_RATE_BANDS)
    return configured_bands(config) & frozenset(RATE_BANDS)


def _percentile(sorted_values: list[float], fraction: float) -> float:
    idx = min(
        len(sorted_values) - 1, max(0, round(fraction * (len(sorted_values) - 1)))
    )
    return sorted_values[idx]


def _count_bucket(n: int) -> str:
    if n == 0:
        return "0"
    return "1-3" if n <= 3 else "4+"


def _slot_entry(prices: list[float], bands: Counter[str], idx: int) -> dict[str, Any]:
    return {
        "slot": _fmt(idx * 30),
        "band": bands.most_common(1)[0][0] if bands else None,
        "n": _count_bucket(len(prices)),
        "median_c_per_kwh": (
            round(_percentile(sorted(prices), 0.5), 2) if prices else None
        ),
    }


def band_profile(
    readings: Iterable[Any],
    raw_types: Mapping[str, int],
    tz: tzinfo,
    rates: Mapping[str, float],
) -> dict[str, Any]:
    """Implied-price profile of one import, for diagnostics (#141 AC9/AC12).

    ``readings`` carry the band each slot was ATTRIBUTED to (``rate_type``).
    Per local half-hour: the majority band, a COARSE count (0 / 1-3 / 4+ —
    an exact per-half-hour count is a weak occupancy signal in a public
    file) and the median implied price; per band: exact count and
    p10/median/p90 implied price beside the configured rate. Prices are
    c/kWh rounded to 2 dp; no kWh value and no timestamp beyond the
    half-hour of day ever appears. ``raw_types`` counts AGL's own
    ``consumption.type`` AFTER the parser (none/pending and zero-on-zero
    slots are already gone).
    """
    by_slot: dict[int, list[float]] = {}
    slot_bands: dict[int, Counter[str]] = {}
    by_band: dict[str, list[float]] = {}
    for r in readings:
        if r.kwh < PROFILE_MIN_KWH:
            continue
        price = r.cost_aud / r.kwh * 100.0
        idx = slot_index(r.dt.astimezone(UTC).astimezone(tz))
        by_slot.setdefault(idx, []).append(price)
        slot_bands.setdefault(idx, Counter())[r.rate_type] += 1
        by_band.setdefault(r.rate_type, []).append(price)
    bands: dict[str, dict[str, Any]] = {}
    for band, prices in sorted(by_band.items()):
        ordered = sorted(prices)
        rate = rates.get(band)
        bands[band] = {
            "n": len(ordered),
            "p10": round(_percentile(ordered, 0.1), 2),
            "median": round(_percentile(ordered, 0.5), 2),
            "p90": round(_percentile(ordered, 0.9), 2),
            "configured_rate_c_per_kwh": (
                round(rate * 100.0, 2) if rate is not None else None
            ),
        }
    return {
        "slots": [
            _slot_entry(by_slot.get(idx, []), slot_bands.get(idx, Counter()), idx)
            for idx in range(SLOTS_PER_DAY)
        ],
        "bands": bands,
        "raw_type_counts": dict(sorted(raw_types.items())),
    }
