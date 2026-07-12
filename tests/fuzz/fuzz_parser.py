"""Atheris fuzz harness for custom_components/haggle/agl/parser.py.

Threat model context (SECURITY.md): TLS pinning is warn-only by design, so
AGL response bodies are attacker-influenceable. The parsers must therefore be
TOTAL over arbitrary JSON — a parser crash is a MITM-triggerable failed poll
cycle. This harness enforces two invariants:

  1. No exception escapes any parse_* function for any json.loads() value.
  2. Every numeric field returned is finite and >= 0 (the _safe_float
     guarantee — protects the recorder's cumulative-sum statistics).

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
from typing import Any

import atheris

# Pre-load the integration package (and its homeassistant import chain)
# outside any instrumentation, then instrument only the functions under
# test — instrumenting the whole HA tree would be prohibitively slow.
import custom_components.haggle  # noqa: F401
from custom_components.haggle.agl import parser

for _fn_name in (
    "parse_overview",
    "parse_interval_readings",
    "parse_daily_readings",
    "parse_bill_period",
    "parse_plan",
    "_classify_tariff",
    "_safe_float",
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


def test_one_input(data: bytes) -> None:
    try:
        obj: Any = json.loads(data)
    except Exception:
        return

    for source_field in ("consumption", "feedIn"):
        for reading in parser.parse_interval_readings(obj, source_field=source_field):
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
