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
| Contract timezone | Included as an IANA zone name (`coordinator.contract_timezone`, `entry.data.local_tz`). This is state-level at coarsest and `Australia/Broken_Hill` (one postcode) at finest — accepted as non-identifying: the zone IS the diagnostic signal for a timestamp report ([#292](https://github.com/NaanyaBiz/haggle/issues/292)). The service address itself is never included. |
| HA core / Python / OS versions | Added automatically by Home Assistant's diagnostics wrapper (`home_assistant` block). |

Schema v2 adds give-up visibility: `stall_give_up_spans` and the
`gave_up` marker on `solar_heal` — each such record is mirrored by an HA
Repairs issue in Settings, so holes announce themselves instead of hiding
behind healthy-looking coverage stats.

Schema v3 adds the timezone triple needed to triage a #292-class
("my hourly bars are 30 min / 1 h early") report: HA's own `timezone`, the
zone the parser corrects AGL's Sydney-converted timestamps into
(`coordinator.contract_timezone`), and the persisted contract-zone key
(`entry.data.local_tz`).

Schema v4 adds `tz_realign`, the record of the one-off timezone history
re-alignment (#292, v0.5.0-beta.5). It moves the last 30 days of rows
written before the correction onto the right hours. With it, a "my older
bars are still early" report can be told apart: re-alignment pending,
re-alignment done, given up, or deliberately not run (NT/WA). Like
`solar_heal`, it is mirrored by HA Repairs issues where the user needs to
know.

## Schema v4 field reference

For the automated triage routine and maintainers. The integration's payload
is under the standard HA wrapper's `"data"` key. `schema_version` gates
parsing — if it is missing or greater than the version documented here,
fall back to treating the file as opaque JSON.

| Field | Meaning | Diagnostic signal |
|---|---|---|
| `schema_version` | Payload shape contract (currently `4`). | Gate parsing on it. |
| `integration.version` | Installed Haggle version. | Satisfies the "Haggle version" triage check. |
| `contract_ref` / `account_ref` | Stable anonymous install identifiers (HMAC-keyed per install). | Correlate multiple reports from the same install. |
| `runtime_available` | Whether setup succeeded far enough to have runtime state. | `false` → the setup failure itself is the bug (auth/network); `coordinator` is `null` and `statistics` empty — don't chase data-shape theories. |
| `timezone` | HA's configured timezone. | Midnight-spike / wrong-day class of bugs are timezone-sensitive. Compare with `coordinator.contract_timezone` — they differ legitimately for a cross-timezone household. |
| `entry.data.local_tz` | Persisted contract-zone IANA key, derived from the service address (`""` = never derived). | `""` on a v0.5.0+ install → a #268-class entry (renamed title, no successful overview yet): it runs on HA's zone with AGL's Sydney-converted timestamps left UNcorrected (deliberately loud) — expect a setup WARNING naming the fallback and, on an SA or QLD-in-DST contract, daily "Dropped N interval(s)" WARNINGs. Differs from `contract_timezone` → setup fell back on a bad key, or the overview refined it this process. |
| `entry.pin_present_auth` / `entry.pin_present_bff` | TOFU TLS pins captured? | `false` → entry predates pinning and neither Reconfigure nor a reauth (which fills missing pins) has run since; a failed capture also leaves a pin unset. |
| `coordinator.contract_timezone` | The zone the parser re-localises AGL's Sydney-converted timestamps into, as resolved at runtime (stored key → HA tz fallback); `null` while runtime is unavailable. | An "hourly bars are early" report with this set to a Sydney-equivalent zone (`Australia/Sydney`, `Melbourne`, `Hobart`) is NOT #292 — the correction is an identity there (ACT addresses resolve to `Australia/Sydney`); look at HA's `timezone` and the dashboard config instead. `Australia/Brisbane` + a complaint confined to October–April, or `Australia/Adelaide`/`Broken_Hill` any time, with history predating the fix → shifted pre-fix rows. Check `tz_realign`: rows older than its window, or every pre-fix row when it gave up or never ran, keep the old timing by design. Never advise deleting statistics. |
| `coordinator.last_update_success` | Did the most recent poll succeed? | `false` → look at auth/network before data-shape theories. Also the *unavailable-vs-unknown* decoder: entities show **Unavailable** exactly when this is `false`; a sensor showing **Unknown** while this is `true` is deliberate gating (mid-backfill, heal cycle), not a failure. |
| `coordinator.last_exception` | Message of the most recent failed update (body-scrubbed at raise time). | Distinguishes rate-limit vs auth vs transport for a `last_update_success: false`; `null` when the last poll succeeded. |
| `coordinator.has_solar` | AGL reports solar on the contract. | `false` + a solar complaint → overview flag problem, not statistics. |
| `coordinator.active_tou_bands` | ToU bands with stored statistics. | Empty on a self-described ToU account → interval tagging or plan problem. |
| `coordinator.bill_period_start` | Start date of the current AGL bill period (last seen). | Required context for any "period sensor ≠ app tile" report — the period sensors measure from this date, the app tile does too, but cumulative sensors don't. |
| `coordinator.data.*` | Latest `HaggleData` snapshot (period totals, rates, cumulative sums, solar period values). | `generation_period_kwh: null` with solar → generation backfill not caught up, **or a heal cycle is suppressing it** — check `solar_heal`. |
| `solar_heal` | One-time generation leading-hole heal record: `{state, floor, attempts}`; `null` if never armed. | `state: "pending"` → period solar sensors are deliberately `unknown` this cycle and a full-window re-import is in progress. `"done"` with a still-short period total → compare `statistics.<generation>.first_date` against `floor`. A `done` record with `gave_up: true` (+ final `attempts`) was reached by give-up, not clean completion — some days are permanently missing. |
| `tz_realign` | One-off timezone history re-alignment record (#292); `null` = never evaluated. While running: `{state: "pending", zone, floor, series, attempts, halts[, last_halt_at][, consumption][, swept_from]}`. Finished: `{state: "done", zone, reason, …}`. `zone` is the contract's IANA key (same as `entry.data.local_tz`). `floor` is the frozen window start (written before the first fetch). Each attempt sweeps from `max(floor, today − 30)` to yesterday. `swept_from` is the effective start of the last completed stage. | **`reason` decodes the outcome.** `"fresh_install"`: no history to move. `"not_affected"`: no shifted day in the window (NSW/VIC/TAS/ACT, QLD outside Sydney DST). `"unsupported_shift"`: **NT/WA exclusion (RA-19)**. The shift exceeds 1 h, nothing ran, history from before v0.5.0-beta.3 keeps the old timing (NT 30/90 min early, WA 2/3 h early), and no Repairs notice appears. That is by design, not a bug. The first corrected poll also double-counted the junction day's leading shift-width of slots (consumption and generation; up to 3 h WA / 90 min NT in Sydney DST), with **no WARNING logged**: this record is the only signal. A fresh NT/WA install shows `"fresh_install"` instead. `"complete"`: done; `consumption`/`generation` say `"complete"`, `"gave_up"` or `"skipped"` per stage. `"gave_up"`: at least one stage gave up; `sweeps` = 3 × the number of stages that gave up, `notice` is `false` and `history_from` is `null`, and one `tz_realign_gave_up_<entry_id>` Repairs issue was raised. The window of a stage that gave up is untouched: old timing, nothing missing, no step, and consumption stays high by the one slot (SA) or hour (QLD) the first corrected poll double-counted at its trailing-week junction (only a complete stage removes it). **`state: "pending"`**: `series: "consumption"` means the consumption window re-imports this cycle. `series: "generation"` means consumption finished (its result is in `consumption`) and the solar window is next. Either stage waits, without counting attempts, while a `solar_heal` sweep runs. A generation stage also waits while `coordinator.has_solar` is `false` but generation rows exist (the overview has not been seen yet this process). Rising `attempts` means AGL errors on old dates (look for DEBUG `Fetch skip YYYY-MM-DD`), or an exception aborted the sweep (see `coordinator.last_exception`). Only per-day errors on days from the window start through `today − 7` count; errors on the six most recent days never do. `halts` counts 429/transport halts, at most one per poll interval; the 3rd converts to one attempt. Stuck pending while `coordinator.contract_timezone` differs from `entry.data.local_tz` (or the latter is `""`): the zone gate is closed. Nothing runs and nothing is counted until the address-derived zone is in force. A different `zone` from `local_tz` means the record is evaluated again once (a real zone change). **`generation: "skipped"`** = no generation history, or solar statistics writes were off when the stage was due (permanent). **`notice`** = whether the `tz_history_before_realign_<entry_id>` Repairs notice was raised (rows older than the effective floor may keep the old timing). **`history_from`** = earliest stored consumption day seen within 366 days before that floor (best-effort; `null` if unknown). Gating and triage only; the notice text never claims it. On a solar contract, a residual of one dawn export slot at the first corrected poll's trailing-week junction is expected even after `"complete"` (CHANGELOG beta.5). After `"complete"`, a floor-day consumption total short by exactly one slot (SA) or hour (QLD with the floor in Sydney DST), with the day before high by the same amount, is **expected**: the junction slot stays counted in the day before's last old-timing row and the cumulative sum is exact. Only on an entry set up on 0.5.0-beta.3+ (already-correct history, so the day before is NOT high by that amount) does the shortfall mean a real slot was dropped, through the accepted junction-check coincidence (threat-model T-5 residual 5). Neither is a fetch failure. |
| `stall_give_up_spans` | List of `{start, end, cycles, gave_up_at}` spans where the normal-path solar backfill gave up (#154) and wrote zero-delta markers; `null` if never. | The ONLY durable evidence of these holes — the marker rows make `first_date`/`row_count` look healthy over them. Any span here + a "solar missing days" report → this is the cause; the days will not self-heal. |
| `statistics.<series>.first_date` | **Earliest** day stored per series. | Later than `bill_period_start` (or the 30-day floor) while `last_date` is current → **leading hole** (#128 class): the resume logic will never revisit those days on its own. |
| `statistics.<series>.last_date` | Most recent day imported per statistics series. | More than ~3 days behind today → fetch stall (rate limit, auth, or zero-day stall classes). A missing series that should exist → that feature never started. |
| `statistics.<series>.row_count` | Stored hourly rows in the series. | Far fewer rows than the `first_date`→`last_date` span implies → interior gaps (transient per-day fetch errors, #145 class). `row_count` does NOT reveal the straddle-bucket class (#292): on a half-hour-zone contract (`contract_timezone` Adelaide / Broken_Hill / Darwin) the pre-guard bug overwrote the day-boundary hourly row with half its kWh, so the row still exists and the count stays healthy — the evidence is a day whose kWh is short by exactly one 30-min slot against the AGL CSV (the CHANGELOG acceptance check), not a missing row. |
| `statistics.<series>.last_sum` | Cumulative sum at the resume point. | Sudden mismatch vs. sensor values → baseline corruption class. |

The `<series>` keys are the real statistic IDs with the contract number
replaced by `contract_ref` — e.g. `haggle:consumption_anon-3f9c2a81d0`.

**When the shape changes:** bump `DIAGNOSTICS_SCHEMA_VERSION` in
`custom_components/haggle/diagnostics.py` and update this table in the same
PR.
