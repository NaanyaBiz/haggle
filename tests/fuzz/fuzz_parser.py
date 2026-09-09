"""Atheris fuzz harness for custom_components/haggle/agl/parser.py.

Threat model context (SECURITY.md): TLS pinning is warn-only by design, so
AGL response bodies are attacker-influenceable. The parsers must therefore be
TOTAL over arbitrary JSON — a parser crash is a MITM-triggerable failed poll
cycle. This harness enforces two invariants:

  1. No exception escapes any parse_* function for any json.loads() value.
  2. Every numeric field returned is finite, >= 0, and <= MAX_AGL_NUMERIC
     (the safe_float guarantee — protects the recorder's cumulative-sum
     statistics). The upper bound is part of the invariant since #241:
     "finite" alone let 1e308 through, and two of those in one hourly
     bucket sum to inf with no exception raised.

Run locally (needs the dev env for the homeassistant import chain):
    uv sync --extra dev
    uv pip install --require-hashes -r tests/fuzz/requirements.txt
    PYTHONPATH=. uv run python tests/fuzz/fuzz_parser.py tests/fixtures

CI: .github/workflows/fuzz.yml — weekly plus on parser/harness changes.
Deterministic crash regressions live in tests/test_parser.py
(TestParserTotality); add one there for every crasher this harness finds.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date, timedelta
from typing import Any

import atheris

# Instrument only the functions under test (instrument_func below):
# atheris.instrument_imports()/instrument_all() would sweep in the whole
# homeassistant import chain and make startup prohibitively slow.
from custom_components.haggle.agl import parser
from custom_components.haggle.const import INTERVAL_DAY_TOLERANCE, MAX_AGL_NUMERIC

# Fixed day for the windowed pass — fuzz inputs may carry any timestamp, and
# the invariant is that everything RETURNED lies within the window.
_FUZZ_EXPECTED_DAY = date(2026, 1, 15)
_FUZZ_WINDOW = (
    _FUZZ_EXPECTED_DAY - timedelta(days=INTERVAL_DAY_TOLERANCE),
    _FUZZ_EXPECTED_DAY + timedelta(days=INTERVAL_DAY_TOLERANCE),
)

for _fn_name in (
    "parse_overview",
    "parse_interval_readings",
    "parse_daily_readings",
    "parse_bill_period",
    "parse_plan",
    "_classify_tariff",
    "safe_float",
    "_as_dict",
    "_as_list",
    "_as_str",
    "_as_id",
):
    setattr(parser, _fn_name, atheris.instrument_func(getattr(parser, _fn_name)))


def _check_amount(value: float) -> None:
    if not math.isfinite(value):
        raise AssertionError(f"non-finite value escaped a parser: {value!r}")
    if value < 0:
        raise AssertionError(f"negative value escaped a parser: {value!r}")
    if value > MAX_AGL_NUMERIC:
        raise AssertionError(f"unbounded value escaped a parser: {value!r}")


def test_one_input(data: bytes) -> None:
    try:
        obj: Any = json.loads(data)
    except Exception:
        return

    for source_field in ("consumption", "feedIn"):
        for reading in parser.parse_interval_readings(obj, source_field=source_field):
            _check_amount(reading.kwh)
            _check_amount(reading.cost_aud)
        # Windowed pass (#242 / T-4): with expected_day set, every RETURNED
        # reading must lie inside the window — a crafted timestamp escaping
        # it is exactly the baseline-cutoff attack the guard exists to stop.
        for reading in parser.parse_interval_readings(
            obj, source_field=source_field, expected_day=_FUZZ_EXPECTED_DAY
        ):
            if not (_FUZZ_WINDOW[0] <= reading.dt.date() <= _FUZZ_WINDOW[1]):
                raise AssertionError(
                    f"out-of-window timestamp escaped the guard: {reading.dt!r}"
                )
            _check_amount(reading.kwh)
            _check_amount(reading.cost_aud)

    for daily in parser.parse_daily_readings(obj):
        _check_amount(daily.kwh)
        _check_amount(daily.cost_aud)

    bill = parser.parse_bill_period(obj)
    _check_amount(bill.consumption_kwh)

    plan = parser.parse_plan(obj)
    _check_amount(plan.supply_charge_cents_per_day)
    for band_price in plan.tou_unit_rates.values():
        _check_amount(band_price)
    if plan.feed_in_rate_cents_per_kwh is not None:
        _check_amount(plan.feed_in_rate_cents_per_kwh)
    for row in plan.unit_rates:
        _check_amount(row["price"])

    parser.parse_overview(obj)


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
