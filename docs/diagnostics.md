# Diagnostics

Haggle supports Home Assistant's built-in diagnostics download. When filing a
bug, attaching this file answers most triage questions (versions, plan type,
solar, backfill state, timezone) in one step.

## How to download

**Settings → Devices & Services → Haggle → ⋮ (three-dot menu) →
Download diagnostics.** Drag the resulting `.json` file into the
"Diagnostics file" box of the
[bug report form](https://github.com/NaanyaBiz/haggle/issues/new?template=bug.yml).

## What is (and isn't) in the file

The file is built to be posted publicly:

| Data | Treatment |
|---|---|
| AGL refresh token | **Redacted** (`**REDACTED**`) — never included. |
| Account number / contract number | **Never included.** Replaced everywhere (including inside statistic IDs and the entry unique_id) by stable anonymous references like `anon-3f9c2a81d0`. References are HMAC-keyed to your Home Assistant install's private instance id, so the same install always produces the same reference (repeat reports correlate) but the number cannot be recovered — not even by brute-forcing all possible 10-digit identifiers, which a bare hash would allow. |
| TLS SPKI pins | Reduced to presence booleans (`pin_present_auth` / `pin_present_bff`). |
| Usage figures, rates, tariff bands, solar flags, timestamps | Included — they are the diagnostic payload and are not personally identifying. |
| HA core / Python / OS versions | Added automatically by Home Assistant's diagnostics wrapper (`home_assistant` block). |

## Schema v1 field reference

For the automated triage routine and maintainers. The integration's payload
is under the standard HA wrapper's `"data"` key. `schema_version` gates
parsing — if it is missing or greater than the version documented here,
fall back to treating the file as opaque JSON.

| Field | Meaning | Diagnostic signal |
|---|---|---|
| `schema_version` | Payload shape contract (currently `1`). | Gate parsing on it. |
| `integration.version` | Installed Haggle version. | Satisfies the "Haggle version" triage check. |
| `contract_ref` / `account_ref` | Stable anonymous install identifiers (HMAC-keyed per install). | Correlate multiple reports from the same install. |
| `runtime_available` | Whether setup succeeded far enough to have runtime state. | `false` → the setup failure itself is the bug (auth/network); `coordinator` is `null` and `statistics` empty — don't chase data-shape theories. |
| `timezone` | HA's configured timezone. | Midnight-spike / wrong-day class of bugs are timezone-sensitive. |
| `entry.pin_present_auth` / `entry.pin_present_bff` | TOFU TLS pins captured? | `false` on both → entry predates pinning or Reconfigure never ran. |
| `coordinator.last_update_success` | Did the most recent poll succeed? | `false` → look at auth/network before data-shape theories. Also the *unavailable-vs-unknown* decoder: entities show **Unavailable** exactly when this is `false`; a sensor showing **Unknown** while this is `true` is deliberate gating (mid-backfill, heal cycle), not a failure. |
| `coordinator.last_exception` | Message of the most recent failed update (body-scrubbed at raise time). | Distinguishes rate-limit vs auth vs transport for a `last_update_success: false`; `null` when the last poll succeeded. |
| `coordinator.has_solar` | AGL reports solar on the contract. | `false` + a solar complaint → overview flag problem, not statistics. |
| `coordinator.active_tou_bands` | ToU bands with stored statistics. | Empty on a self-described ToU account → interval tagging or plan problem. |
| `coordinator.bill_period_start` | Start date of the current AGL bill period (last seen). | Required context for any "period sensor ≠ app tile" report — the period sensors measure from this date, the app tile does too, but cumulative sensors don't. |
| `coordinator.data.*` | Latest `HaggleData` snapshot (period totals, rates, cumulative sums, solar period values). | `generation_period_kwh: null` with solar → generation backfill not caught up, **or a heal cycle is suppressing it** — check `solar_heal`. |
| `solar_heal` | One-time generation leading-hole heal record: `{state, floor, attempts}`; `null` if never armed. | `state: "pending"` → period solar sensors are deliberately `unknown` this cycle and a full-window re-import is in progress. `"done"` with a still-short period total → compare `statistics.<generation>.first_date` against `floor`. |
| `statistics.<series>.first_date` | **Earliest** day stored per series. | Later than `bill_period_start` (or the 30-day floor) while `last_date` is current → **leading hole** (#128 class): the resume logic will never revisit those days on its own. |
| `statistics.<series>.last_date` | Most recent day imported per statistics series. | More than ~3 days behind today → fetch stall (rate limit, auth, or zero-day stall classes). A missing series that should exist → that feature never started. |
| `statistics.<series>.row_count` | Stored hourly rows in the series. | Far fewer rows than the `first_date`→`last_date` span implies → interior gaps (transient per-day fetch errors, #145 class). |
| `statistics.<series>.last_sum` | Cumulative sum at the resume point. | Sudden mismatch vs. sensor values → baseline corruption class. |

The `<series>` keys are the real statistic IDs with the contract number
replaced by `contract_ref` — e.g. `haggle:consumption_anon-3f9c2a81d0`.

**When the shape changes:** bump `DIAGNOSTICS_SCHEMA_VERSION` in
`custom_components/haggle/diagnostics.py` and update this table in the same
PR.
