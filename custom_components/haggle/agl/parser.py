"""NEM12 -> coordinator-data parser.

NEM12 is the Australian Energy Market Operator's standard for half-hourly
interval-meter data. AGL exports it as CSV; we sum the relevant intervals
into cumulative kWh totals suitable for `state_class=total_increasing`.

Stub. Real implementation lands in Sprint 1; reference parsing logic at
https://github.com/aarond10/agl_to_nem12.
"""

from __future__ import annotations

from typing import Any


def parse_nem12(_csv: str) -> dict[str, Any]:
    """Parse a NEM12 CSV export into coordinator data."""
    raise NotImplementedError("energy-domain-expert: implement in Sprint 1")
