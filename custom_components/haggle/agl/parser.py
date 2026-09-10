"""Parsers: AGL JSON API responses -> domain dataclasses.

Reference response shapes are mirrored under tests/fixtures/ (anonymised).
Field semantics are documented in AGENTS.md §AGL API — Key Facts.

Critical fields:
  - Interval kWh:  consumption.quantity        (outer — matches AEMO/CSV)
  - Interval cost: consumption.amount          (outer — AUD for the slot)
  - Interval dt:   dateTime field is slot-start in UTC
  - Contract ID:   contractNumber (not accountNumber, not accountId)

The inner ``consumption.values.{amount,quantity}`` block is a DPI/chart-scaled
helper (the two sub-fields are always equal in real responses) and is NOT
the metered value. Reading it undercounts kWh by 4-73% with no consistent
ratio — confirmed by reconciling 11 mitm /Hourly captures against an AGL
"MyUsageData" portal CSV export, 2026-05-12.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Callable

from ..const import (
    INTERVAL_DAY_TOLERANCE,
    INTERVAL_WINDOW_TRAILING_SLACK_HOURS,
    MAX_AGL_NUMERIC,
    TARIFF_OFFPEAK,
    TARIFF_PEAK,
    TARIFF_SHOULDER,
)
from .models import BillPeriod, Contract, DailyReading, IntervalReading, PlanRates

_LOGGER = logging.getLogger(__name__)


def _classify_tariff(text: str) -> str | None:
    """Map a plan rate's header/title text to a ToU tariff band, or None.

    Heuristic — AGL's plan endpoint groups c/kWh detail rows under free-text
    `kind:"header"` rows (e.g. "Peak", "Off Peak", "Shoulder") rather than a
    machine tariff-type field. Order matters: "off peak" must be tested before
    the bare "peak" substring. Returns None for general/flat usage rows so an
    unmatched band surfaces as `unavailable`, never a misleading 0.0. The
    keyword mapping is extrapolated from the documented response shape (see
    AGENTS.md §AGL API).
    """
    t = text.lower()
    if "shoulder" in t:
        return TARIFF_SHOULDER
    if "off peak" in t or "off-peak" in t or "offpeak" in t:
        return TARIFF_OFFPEAK
    if "peak" in t:
        return TARIFF_PEAK
    return None


class NumericRejections:
    """Counter out-param for :func:`safe_float` at batch call sites.

    A crafted interval response can carry thousands of over-bound values —
    two per item — and a synchronous WARNING for each would stall the event
    loop and flood the log through the same MITM-influenceable surface the
    guard defends (Codex finding on PR #266; same class as the out-of-window
    timestamp counter in parse_interval_readings). Batch parsers pass one of
    these, count silently, and emit a single bounded summary after the loop.
    """

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0


def safe_float(raw: Any, *, rejections: NumericRejections | None = None) -> float:
    """Coerce a raw API value to a non-negative, finite float <= MAX_AGL_NUMERIC.

    inf/nan/negative and over-bound values become 0.0 (never clamped to the
    bound — a zero delta leaves a cumulative sum untouched; a clamped 1e6
    would write a permanent false spike). Single implementation (#241);
    coordinator.py imports it. Full rationale: AGENTS.md "What NOT to Do".

    With ``rejections`` given, a rejection is counted instead of logged —
    the caller emits ONE summary. Without it (single-value call sites: plan
    rates, usage summary), the per-call WARNING stands, with the repr
    truncated so hostile content can't flood a single log line.
    """
    try:
        value = float(raw or 0.0)
    except TypeError, ValueError:
        return 0.0
    if not math.isfinite(value) or value < 0:
        if rejections is not None:
            rejections.count += 1
        else:
            _LOGGER.warning("Rejecting non-finite/negative AGL value: %.60r", raw)
        return 0.0
    if value > MAX_AGL_NUMERIC:
        if rejections is not None:
            rejections.count += 1
        else:
            _LOGGER.warning("Rejecting implausibly large AGL value: %.60r", raw)
        return 0.0
    return value


# --- totality guards -------------------------------------------------------
# AGL envelopes are dicts/lists/strings at known positions, but response
# bodies are attacker-influenceable (TLS pinning is warn-only by design), so
# every parser must be TOTAL over arbitrary JSON: malformed shapes degrade to
# empty/default results, never raise. Enforced by tests/fuzz/fuzz_parser.py
# and TestParserTotality in tests/test_parser.py.


def _as_dict(raw: Any) -> dict[str, Any]:
    """Return raw if it is a dict, else {}."""
    if isinstance(raw, dict):
        return cast("dict[str, Any]", raw)
    return {}


def _as_list(raw: Any) -> list[Any]:
    """Return raw if it is a list, else []."""
    if isinstance(raw, list):
        return raw
    return []


def _as_str(raw: Any, default: str = "") -> str:
    """Return raw if it is a str, else default."""
    return raw if isinstance(raw, str) else default


def _as_id(raw: Any) -> str:
    """Account/contract identifiers: accept str (or int, coerced), else ''."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, int) and not isinstance(raw, bool):
        return str(raw)
    return ""


def parse_overview(data: dict[str, Any]) -> list[Contract]:
    """Parse /api/v3/overview response.

    A contract entry without a usable contractNumber is skipped — a
    malformed (or tampered) entry must drop out, not crash discovery.
    """
    contracts: list[Contract] = []
    for account_raw in _as_list(_as_dict(data).get("accounts")):
        account = _as_dict(account_raw)
        account_number = _as_id(account.get("accountNumber"))
        address = _as_str(account.get("address"))
        for c_raw in _as_list(account.get("contracts")):
            c = _as_dict(c_raw)
            contract_number = _as_id(c.get("contractNumber"))
            if not contract_number:
                continue
            contracts.append(
                Contract(
                    contract_number=contract_number,
                    account_number=account_number,
                    address=address,
                    fuel_type=_as_str(c.get("type")),
                    status=_as_str(c.get("status")),
                    has_solar=bool(c.get("hasSolar", False)),
                    meter_type=_as_str(c.get("meterType"), "smart"),
                )
            )
    return contracts


# Australian state/territory (as it appears before the postcode in an AGL
# service address, e.g. "1 Sample Street SUBURB QLD 4000") → IANA timezone.
# ACT shares Sydney's rules.
_STATE_TZ: dict[str, str] = {
    "NSW": "Australia/Sydney",
    "ACT": "Australia/Sydney",
    "VIC": "Australia/Melbourne",
    "QLD": "Australia/Brisbane",
    "SA": "Australia/Adelaide",
    "TAS": "Australia/Hobart",
    "NT": "Australia/Darwin",
    "WA": "Australia/Perth",
}
# State token immediately followed by a 4-digit postcode — anchoring on the
# pair avoids false-positives on street/suburb words.
_ADDRESS_STATE_RE = re.compile(r"\b(NSW|ACT|VIC|QLD|SA|TAS|NT|WA)\s+(\d{4})\b")

# Sub-state timezone exceptions, postcode-keyed. Broken Hill (+9:30/+10:30)
# runs 30 min behind Sydney time, so the state-level zone would re-open a
# 30-minute leading window on the baseline cutoff there (Codex pass 5,
# PR #266). Remaining micro-exceptions (Lord Howe Island, Eucla) are
# recorded as an accepted residual on the follow-up issue — populations
# AGL is unlikely to serve, and the counted-drop WARNING is the tripwire.
_POSTCODE_TZ: dict[tuple[str, str], str] = {
    ("NSW", "2880"): "Australia/Broken_Hill",
}


def tz_for_address(address: str) -> tzinfo | None:
    """Best-effort timezone of an AGL service address, or None.

    The interval-timestamp window must be the CONTRACT's local day, not the
    HA instance's (Codex pass-3 P1, PR #266): a Sydney contract managed from
    a Brisbane HA host is off by 1 h during DST, and the window's strict
    lower bound would then drop the day's first slots on every fetch —
    permanent undercount. The state token before the postcode is the best
    contract-locality signal the API exposes. None on no match (or missing
    tzdata) — callers keep their existing fallback. Totality is inherited:
    parse_overview coerces address via _as_str, so input is always str.
    """
    match = _ADDRESS_STATE_RE.search(address)
    if match is None:
        return None
    state, postcode = match.group(1), match.group(2)
    key = _POSTCODE_TZ.get((state, postcode)) or _STATE_TZ.get(state)
    if key is None:
        return None
    try:
        return ZoneInfo(key)
    except KeyError, OSError:
        return None


def _out_of_window_predicate(
    expected_day: date | None, tz: tzinfo | None
) -> Callable[[datetime], bool]:
    """Build the reject-predicate for the requested-day window (#242).

    Window semantics are documented on parse_interval_readings; this exists
    so the parse loop stays under the complexity gate (decompose, not noqa).
    """
    if expected_day is None:
        return lambda _dt: False
    if tz is not None:
        day_start = datetime(
            expected_day.year, expected_day.month, expected_day.day, tzinfo=tz
        )
        next_day = expected_day + timedelta(days=1)
        day_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=tz)
        # Trailing slack ONLY: the baseline cutoff is min(hour_cons), so a
        # leading slack of any width re-admits the cutoff attack at that
        # width (Codex pass-2 P1, PR #266); a late row cannot lower the min.
        slack = timedelta(hours=INTERVAL_WINDOW_TRAILING_SLACK_HOURS)
        lo, hi = day_start, day_end + slack
        return lambda dt: not (lo <= dt < hi)
    lo_date = expected_day - timedelta(days=INTERVAL_DAY_TOLERANCE)
    hi_date = expected_day + timedelta(days=INTERVAL_DAY_TOLERANCE)
    return lambda dt: not (lo_date <= dt.date() <= hi_date)


def parse_interval_readings(
    data: dict[str, Any],
    *,
    source_field: str = "consumption",
    expected_day: date | None = None,
    tz: tzinfo | None = None,
) -> list[IntervalReading]:
    """Parse /Hourly response into 30-min interval readings.

    Filters out items with type='none' or type='pending' (future/unavailable
    slots) and placeholder slots where kWh and cost are both zero (AGL returns
    these for days where AEMO meter reads have not yet been delivered, even with
    a non-``none`` type — they would otherwise create phantom flat rows in the
    statistics table that the resume logic would never re-check).
    dateTime is slot-start UTC; kwh from consumption.quantity (outer).

    ``source_field`` selects which per-item block to read. The default
    "consumption" covers the Electricity endpoint; the ElectricitySolar
    endpoint additionally carries a shape-identical "feedIn" block (exported
    kWh in the outer ``quantity``, AUD feed-in credit in the outer ``amount``)
    — pass source_field="feedIn" to extract it. Zero-on-zero feedIn slots are
    real (no sun at night), but dropping them is still correct: a zero delta
    never moves the cumulative sum, and the trailing rewindow re-visits the
    hour anyway.

    ``expected_day`` is the day actually requested via ``period=``; readings
    outside its window are dropped (#242) so a crafted timestamp can't pin
    the baseline cutoff before real recorder history (threat-model T-4, the
    #114 sum-step class). With ``tz`` — the contract's local timezone, which
    the caller asserts is the HA instance's configured one (the same
    assumption every local-midnight computation in the coordinator makes) —
    the window is exact: [local midnight of the day, next local midnight +
    trailing slack) in UTC, DST handled by the tzinfo. AGL reads
    ``period=`` in LOCAL time and returns UTC, so this is the true shape of
    one requested day. Without ``tz`` the fallback is the coarser
    ``expected_day ± INTERVAL_DAY_TOLERANCE`` DATE window — Codex P1 on PR
    #266 showed that window alone still admits an injected D-1T00:00Z
    reading that drags the baseline cutoff ~14 h early, excluding stored
    rows from the baseline without re-emitting them: a #114 downward step.
    """
    _skip_types = {"none", "pending"}
    out_of_window = _out_of_window_predicate(expected_day, tz)
    readings: list[IntervalReading] = []
    dropped_out_of_window = 0
    rejections = NumericRejections()
    for section_raw in _as_list(_as_dict(data).get("sections")):
        for item_raw in _as_list(_as_dict(section_raw).get("items")):
            item = _as_dict(item_raw)
            block = _as_dict(item.get(source_field))
            rate_type = block.get("type", "none")
            # A non-str type can't name a tariff series (and an unhashable
            # one would blow up the set membership) — treat as malformed.
            if not isinstance(rate_type, str) or rate_type in _skip_types:
                continue
            dt_str = item.get("dateTime", "")
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
            except ValueError, AttributeError:
                continue
            # Counted, not logged per-item: a hostile response could carry
            # thousands of these, and a synchronous WARNING per reading
            # would stall the event loop and flood the log (review finding).
            if out_of_window(dt):
                dropped_out_of_window += 1
                continue
            kwh = safe_float(block.get("quantity"), rejections=rejections)
            cost_aud = safe_float(block.get("amount"), rejections=rejections)
            if kwh == 0.0 and cost_aud == 0.0:
                continue
            readings.append(
                IntervalReading(
                    dt=dt,
                    kwh=kwh,
                    cost_aud=cost_aud,
                    rate_type=rate_type,
                )
            )
    if dropped_out_of_window:
        _LOGGER.warning(
            "Dropped %d interval(s) outside the window for requested day %s",
            dropped_out_of_window,
            expected_day,
        )
    if rejections.count:
        _LOGGER.warning(
            "Rejected %d non-finite/negative/over-bound numeric value(s) "
            "in interval response for %s",
            rejections.count,
            expected_day,
        )
    return readings


def parse_daily_readings(data: dict[str, Any]) -> list[DailyReading]:
    """Parse /Daily response.

    Response uses sections[].items[] — same envelope as /Hourly.
    dateTime is day-start in UTC (time component is 00:00:00Z).
    kWh from consumption.quantity (outer — see module docstring).
    """
    readings: list[DailyReading] = []
    for section_raw in _as_list(_as_dict(data).get("sections")):
        for item_raw in _as_list(_as_dict(section_raw).get("items")):
            item = _as_dict(item_raw)
            consumption = _as_dict(item.get("consumption"))
            rate_type = consumption.get("type")
            # str-only membership test: an unhashable type value (dict/list)
            # must not raise; absent/odd types keep the original keep-path.
            if isinstance(rate_type, str) and rate_type in {"none", "pending"}:
                continue
            dt_str = item.get("dateTime", "")
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                day: date = dt.date()
            except ValueError, AttributeError:
                continue
            kwh = safe_float(consumption.get("quantity"))
            cost_aud = safe_float(consumption.get("amount"))
            if kwh == 0.0 and cost_aud == 0.0:
                continue
            readings.append(DailyReading(day=day, kwh=kwh, cost_aud=cost_aud))
    return readings


def parse_bill_period(data: dict[str, Any]) -> BillPeriod:
    """Parse /usage summary response.

    Path: data["billPeriod"]["current"].
    """
    payload = _as_dict(data)
    current = _as_dict(_as_dict(payload.get("billPeriod")).get("current"))

    start_str = _as_str(_as_dict(current.get("start")).get("date"))
    end_str = _as_str(_as_dict(current.get("end")).get("date"))
    today_utc = datetime.now(UTC).date()
    try:
        start = date.fromisoformat(start_str)
    except ValueError, TypeError:
        start = today_utc
    try:
        end = date.fromisoformat(end_str)
    except ValueError, TypeError:
        end = today_utc

    usage = _as_dict(current.get("usage"))
    cost_label = _as_str(usage.get("amount"), "$0.00")

    # projection is in the overview response but not in the usage summary;
    # return empty string if absent — callers can populate from overview.
    projection_label = _as_str(payload.get("additionalLabelValue"))

    # quantity is usually a label ("1,234.5 kWh"); a bare JSON number, empty
    # or whitespace-only string must degrade to 0.0, never crash
    # (whitespace previously hit .split()[0] -> IndexError; fuzz-enforced).
    quantity_raw = usage.get("quantity")
    if isinstance(quantity_raw, int | float) and not isinstance(quantity_raw, bool):
        consumption_kwh = safe_float(quantity_raw)
    else:
        parts = _as_str(quantity_raw, "0").replace(",", "").split()
        consumption_kwh = safe_float(parts[0] if parts else 0.0)

    return BillPeriod(
        start=start,
        end=end,
        consumption_kwh=consumption_kwh,
        cost_label=cost_label,
        projection_label=projection_label,
    )


def parse_plan(data: dict[str, Any]) -> PlanRates:
    """Parse /api/v2/plan/energy/{contractNumber} response."""
    payload = _as_dict(data)
    product_name = _as_str(payload.get("productName"))
    unit_rates: list[dict[str, Any]] = []
    supply_charge: float = 0.0
    tou_unit_rates: dict[str, float] = {}
    # AGL groups detail rows under free-text `kind:"header"` rows; track the
    # most recent header so a ToU band ("Peak"/"Off Peak"/"Shoulder") can be
    # inferred even when the per-rate title is generic ("First N kWh").
    current_header = ""

    for rate_raw in _as_list(payload.get("gstInclusiveRates")):
        rate = _as_dict(rate_raw)
        kind = rate.get("kind")
        if kind == "header":
            current_header = _as_str(rate.get("title"))
            continue
        if kind != "detail":
            continue
        rate_type = _as_str(rate.get("type"))
        price = safe_float(rate.get("price"))
        title = _as_str(rate.get("title"))
        if rate_type == "c/day" and "supply" in title.lower():
            supply_charge = price
        if rate_type == "c/kWh":
            band = _classify_tariff(f"{current_header} {title}")
            # First matching rate per band wins; AGL repeats the same c/kWh
            # across tiered "First N kWh" / "Thereafter" rows.
            if band is not None and band not in tou_unit_rates:
                tou_unit_rates[band] = price
        # Allowlist the four fields the coordinator actually consumes — drops
        # any extra keys an attacker-controlled (MITM) response could inject.
        unit_rates.append(
            {
                "kind": kind,
                "type": rate_type,
                "title": title,
                "price": price,
            }
        )

    # Solar feed-in tariff lives in gstExclusiveRates (FiT is GST-free, so
    # AGL correctly reports it there — confirmed from a real solar plan
    # capture on #128). Matched by title so an unrelated GST-exclusive row
    # can never masquerade as the feed-in rate; unmatched plans stay None
    # (sensor reads `unavailable`, never a misleading 0.0).
    feed_in_rate: float | None = None
    for rate_raw in _as_list(payload.get("gstExclusiveRates")):
        rate = _as_dict(rate_raw)
        if rate.get("kind") != "detail" or rate.get("type") != "c/kWh":
            continue
        title = _as_str(rate.get("title"))
        if "feed-in" in title.lower() or "feed in" in title.lower():
            feed_in_rate = safe_float(rate.get("price"))
            break

    return PlanRates(
        product_name=product_name,
        unit_rates=unit_rates,
        supply_charge_cents_per_day=supply_charge,
        tou_unit_rates=tou_unit_rates,
        feed_in_rate_cents_per_kwh=feed_in_rate,
    )
