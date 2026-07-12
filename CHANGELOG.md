# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- **Scorecard remediation batch verified** (#179): aggregate **7.0 → 7.3**
  on the post-release run — Fuzzing 0→10 (atheris harness credited),
  Token-Permissions 9→10, Signed-Releases −1→8 (v0.4.0-beta.5 is the first
  release with an attested zip asset; attestation verified end-to-end),
  Branch-Protection −1→3 (`protect-main` ruleset readable; the higher
  tiers require human approvers — accepted for a solo-maintained repo).
  All nine regression-watch checks held at 10. SECURITY.md "Own Scorecard
  posture" rewritten to the new state. Remaining movers: bestpractices.dev
  badge (#172) and the `Maintained` repo-age gate (~Aug 2026).
- **Release tags are now SSH-signed**, and local commits are signed by
  default (`gpg.format ssh`). Server-side signed-commit enforcement is
  deliberately NOT enabled — squash merges to `main` are already
  GitHub-signed, and remote agent sessions cannot hold the key.

### Changed

- **Release flow updated for the `protect-main` ruleset** (`/release` +
  `release-manager`): the version bump now goes via a short-lived PR;
  the signed tag is created on the squash-merge commit and pushed
  separately. The old direct-commit-to-main flow bounces off the ruleset.

### Targets for next sprint

- #141 — user-configured ToU windows: derive tariff bands locally from
  interval timestamps instead of trusting `consumption.type` (decision on
  #126, 2026-07-03).
- #90 — validate the ToU plan rate-mapping heuristic against a real ToU plan
  capture (reduced priority: #141's manual rate entry demotes the heuristic
  to a default-prefill role).

---

## [0.4.0-beta.5] — 2026-07-12

> **Pre-release, security/supply-chain batch** (2026-07 dependency review +
> OpenSSF Scorecard remediation). No runtime feature changes — parser
> hardening, continuous fuzzing, and the first release shipping an attested
> zip asset with Sigstore provenance.

### Security

- **AGL response parsers are now total over arbitrary JSON** (#173): a
  MITM-crafted or corrupt body can no longer crash a poll cycle via the
  parser layer. Fixed crash classes — whitespace/numeric `usage.quantity`
  (`IndexError`/`AttributeError` in `parse_bill_period`), non-dict nodes at
  any envelope level (`.get` on list/str/int), unhashable
  `consumption.type` values (`TypeError` on set membership), missing
  `contractNumber` (`KeyError` — entry now skipped), and non-str plan
  titles (`.lower()` on int). Regression-pinned in
  `tests/test_parser.py::TestParserTotality`.
- **Continuous fuzzing of the parsers** (#173): atheris harness
  (`tests/fuzz/fuzz_parser.py`) asserts totality plus the finite/
  non-negative numeric guarantee, seeded from the anonymised fixtures;
  runs weekly and on parser changes (`fuzz.yml`, hash-pinned install).
  Addresses the OpenSSF Scorecard Fuzzing check.

Supply-chain surface reduction from the 2026-07 dependency review (no
user-facing impact — the integration still ships zero pip requirements):

- **Codecov upload removed from CI.** The upload was write-only (no badge,
  no consumer) while running third-party vendor code on every push/PR.
  Coverage still prints inline via `--cov-report=term-missing`.
- **Release workflow now uses the first-party `gh` CLI** instead of
  `softprops/action-gh-release` — no third-party code runs in the only
  workflow holding `contents: write` + `id-token: write`.
- **Single lint/type toolchain.** The pre-commit ruff/mypy hooks now run
  `uv run ruff` / `uv run mypy` (`language: system`), so the versions come
  from `uv.lock` — the tag-pinned mirror hooks had drifted to ruff v0.7.4 /
  mypy v1.13.0 against locked 0.15.20 / 2.2.0, a standing local-vs-CI
  disagreement. Removes the orphaned `types-beautifulsoup4` stub (zero bs4
  call sites since v0.1.x) and fixes the stale `py313`/`python3.13`
  remnants (`ruff target-version = "py314"`).
- **Pre-commit hook revs frozen to commit SHAs** (were mutable tags — the
  last unpinned code-fetch path in the repo). Refresh with
  `pre-commit autoupdate --freeze`.
- **OpenSSF Scorecard workflow + README badge** (`scorecard.yml`,
  SHA-pinned, `publish_results: true`). At review time the repo's direct
  dependencies measured 5.4–7.4; the repo itself attests releases — a check
  none of its dependencies pass.
- **Releases now ship an attested artifact**: `release.yml` builds
  `haggle-<ver>.zip` and uploads it with its Sigstore provenance bundle
  (`.zip.sigstore`) as release assets (verify:
  `gh attestation verify haggle-<ver>.zip --repo NaanyaBiz/haggle`). HACS
  install path unchanged (`zip_release` stays false). Also adds the
  missing top-level `permissions: {}` to `release.yml` (Scorecard
  Token-Permissions 9→10). First Scorecard self-assessment: **7.0/10**;
  the remaining deductions are triaged in `SECURITY.md` ("Own Scorecard
  posture") with follow-ups in #171 (ruleset), #172 (Best Practices
  badge), #173 (parser fuzzing).
- **Threat model updated**: `SECURITY.md` "Supply chain" now records the
  full posture — zero shipped requirements and the dev-only bump risk
  calculus, the 167-package lockfile attribution (~90% HA-ecosystem tax,
  hash-pinned), no-third-party-actions-in-privileged-workflows rule, the
  accepted branch-SHA pins (`hacs/action`, `home-assistant/actions`), and
  the diminishing-returns line (what is deliberately kept).

### Changed

- **Dependabot now ignores `pytest`** (`dependabot.yml`): its effective
  version is exact-pinned inside `pytest-homeassistant-custom-component`
  (0.13.346 → `pytest==9.0.3`), so independent floor bumps install
  nothing different and a floor above phcc's pin deadlocks the whole
  grouped update (#120 replayed on #170). pytest upgrades arrive via
  phcc's own bumps.
- **Dependabot PRs are now grouped** (one weekly PR per ecosystem instead
  of ~8 individual ones — the previous flow hand-batched them anyway; 8 of
  the repo's 50 commits were dependency-maintenance churn).
- **`pytest-homeassistant-custom-component` widened to a range**
  (`>=0.13.344,<0.14`, was an exact patch pin). Upstream releases
  near-daily, so the exact pin manufactured a guaranteed weekly bump PR
  and caused two resolver deadlocks (#106, #120). `uv.lock` remains the
  reproducibility authority.

---

## [0.4.0-beta.4] — 2026-07-09

> **Pre-release for community validation** (#128 round 4 / resilience batch).
> First release carrying the **diagnostics platform** — bug reports can now
> attach an anonymized *Download diagnostics* file — plus five coordinator
> resilience fixes hardened across two adversarial review rounds. If you
> upgraded through beta.1 on a solar contract: the one-time history heal now
> also shows *Solar sold this period* the same cycle it finishes, instead of
> blanking it for a day or more.

### Added

- **Diagnostics platform** (`diagnostics.py`): the integration card now has a
  **Download diagnostics** button producing an anonymized JSON for bug
  reports — refresh token redacted; account/contract numbers replaced
  everywhere (statistic IDs and unique_id included) by stable `anon-…`
  references; SPKI pins reduced to booleans. Payload carries integration
  version, timezone, coordinator state (plan type, ToU bands, solar flags,
  period totals, bill-period start, last update error), the one-time solar
  heal record, and per-series statistics **coverage** (`first_date`,
  `last_date`, `row_count`, `last_sum`) — the earliest-row field is what
  makes a #128-class leading hole visible at a glance. Versioned by
  `schema_version` (see `docs/diagnostics.md`); the bug-report form now asks
  for the file.
- **Faster recovery from failed polls** (#155): a poll that fails with a
  transient AGL error now retries after 30 minutes instead of silently
  waiting a full 24 h (which looked exactly like "the poll never ran",
  #126). The 24 h cadence restores on the next success; auth failures still
  hand over to the reauth flow untouched.
- **Bounded give-up for stalled backfill chunks** (#154): a contiguous span
  of permanently-erroring solar days no longer refetches the identical
  chunk forever — after 3 consecutive zero-progress cycles the span is
  marked as covered (with a WARNING) so the backfill moves on. Rate-limited
  sweeps never count toward the give-up.

### Fixed

- **AGL client transport shield** (#151): network errors, timeouts, and a
  200 response with a non-JSON body (e.g. a bot-protection challenge page)
  now surface as retryable `AGLError` instead of crashing the whole update
  cycle. Previously a deterministic transport failure on an old
  heal-window day could re-fire an unbounded 30-day sweep every cycle with
  the integration unavailable throughout — the heal's give-up cap never
  engaged because the crash bypassed its attempt accounting. Belt-and-
  braces: a heal attempt is now counted on *any* sweep exit, even for
  exception types the client fails to wrap. A network blip during token
  refresh is likewise retryable and no longer masquerades as an auth
  failure.
- **Period solar sensors no longer go blank during the one-time heal**
  (#152): a heal sweep that completes cleanly now waits for the recorder to
  commit the rewritten rows and then publishes *Solar sold this period* /
  *feed-in credit this period* the same cycle — no more 1–3 days of
  `unknown` after upgrading. Incomplete sweeps (rate-limited or skipped
  days) stay suppressed: a wrong number is worse than a blank one.
- **Broken-chain repair survives heal give-up** (#153): a downward
  cumulative-sum step frozen by a rate limit on the heal's final attempt is
  now detected after the heal is done and repaired by one bounded repair
  generation (fresh attempt budget, marked `repair` so it can never loop).
  Lifetime sweeps are hard-capped either way.

### Security

- Dev lockfile refreshed, clearing 19 of 24 open Dependabot alerts (aiohttp
  3.13.5 → 3.14.1, cryptography 47.0.0 → 48.0.1, homeassistant 2026.5.1 →
  2026.7.0, zeroconf 0.148.0 → 0.150.0, uv 0.11.8 → 0.11.25). The committed
  `uv.lock` had gone stale against the `homeassistant>=2026.7.0` floor.
  **No user impact either way**: `manifest.json` ships zero Python
  requirements — users get all of these libraries from their Home Assistant
  core install, never from this integration. The remaining 5 alerts (PyJWT ≤
  2.12.1) are pinned exactly by HA core and sit in code paths this repo never
  imports (token-expiry decoding is a hand-rolled base64 of the JWT payload);
  dismissed with reasons, will clear when HA bumps PyJWT upstream.
- `aiohttp` dev floor raised to `>=3.14.1` (HA 2026.7.0 relaxed its exact
  pin), superseding Dependabot #106.

---

## [0.4.0-beta.3] — 2026-07-07

> **Pre-release for community validation** (#128 round 3). Ships the solar
> generation **leading-hole heal** for installs that upgraded through beta.1:
> those seeded their generation series from only the trailing rewindow, so the
> older billing-period days were never fetched and *Solar sold this period*
> undercounted the AGL app. This build re-imports the full window once to
> backfill them. If you upgraded from beta.1 on a solar contract, please
> confirm *Solar sold this period* and the Energy dashboard "Return to grid"
> bars line up with the app after a poll cycle or two.

### Fixed

- Solar generation: heal a **leading hole** in the generation statistics for
  contracts upgraded from beta.1 (#128). Beta.1 seeded the generation series
  from the *consumption* resume point, so on a caught-up install solar imported
  only the trailing `REWINDOW_DAYS` and the older billing-period days were never
  fetched — `_resolve_fetch_start` keys off the series' last row and never
  revisits them, permanently stranding (for the reporter) 24–27 June (~27 kWh)
  and making *Solar sold this period* undercount the AGL app. The coordinator
  now detects a leading hole (earliest stored row well after the backfill floor)
  and re-imports the full window in one contiguous batch via the existing hourly
  endpoint, so the cumulative chain is recomputed from a correct baseline. The
  heal is a one-time repair whose progress is recorded in the config entry
  (`solar_heal` = `{state, floor, attempts}`): the backfill floor is **frozen**
  when the heal starts so a rate-limited retry re-fetches the same window
  instead of sliding forward and dropping its oldest day; it stays *pending* —
  retrying — while any day was skipped (429 or a transient AGL error), up to a
  few attempts, then *done* and never re-runs, so an interrupted heal finishes
  and a permanently-erroring old day can't wedge it or re-sweep every poll.
  Bill-period solar totals are suppressed for a heal cycle (avoiding a transient
  over-read while the rewritten rows are still queued). Fresh installs and
  flat/ToU/non-solar contracts are unaffected.

### Documentation

- **Energy dashboard setup guide** (`docs/energy-dashboard.md`): which
  `haggle:…` statistics to add per plan type (flat / ToU / solar), sensor
  glossary, data-timing expectations, and troubleshooting — including the
  "whole day as one bar on the wrong date" symptom caused by charting a
  `sensor.…` entity instead of a statistic (#137, also observed on #126).
- README refreshed: HACS default-store install (no more custom-repository
  steps), current status (ToU in validation, solar in beta), new Energy
  dashboard and Solar sections. `info.md` no longer tells users the
  cumulative Consumption sensor "fits the Grid consumption slot" — that
  instruction was the #137 footgun.

---

## [0.4.0-beta.2] — 2026-07-06

> **Pre-release for community validation** (#128 round 2). The beta.1 solar
> field mapping is now **capture-validated**: on the tester's real 2026-07-01
> capture the parser reproduces the AGL app's figures exactly (sold to grid
> 8.019 kWh / $1.363 vs the app's 8.02 / $1.36; consumption 6.072 / $2.254 vs
> 6.07 / $2.25). The mismatch reported against beta.1 was a *window* artifact
> — a cumulative-since-backfill sensor compared against the app's
> billing-period tile — not a data bug. Beta.2 adds bill-period sensors that
> match the app tile directly.

### Added

- **Bill-period solar sensors** (#128): **Solar sold this period** (kWh) and
  **Solar feed-in credit this period** (AUD) — computed from the generation
  statistics since the current bill period start, so they line up with the
  AGL app's "Sold To Grid" tile. They read `unknown` until the generation
  series has backfilled to the trailing rewindow (a mid-backfill partial
  could never match the app). Note: on quarterly billing the 30-day backfill
  window is shorter than the bill period, so these sensors cover the stored
  history only.
- **Solar feed-in rate sensor** (#128): AGL reports the feed-in tariff under
  `gstExclusiveRates` (FiT is GST-free) — the parser previously only read
  `gstInclusiveRates`, so the rate never surfaced. New **Solar feed-in rate**
  sensor (AUD/kWh) registers on solar contracts when the plan carries a
  feed-in rate.

### Fixed

- **Generation series now backfills independently of consumption** (#128).
  Previously the fetch window was resolved from the consumption series'
  resume point alone, so an install that *upgraded* into solar support only
  ever received the trailing 7-day rewindow of export history — the older
  ~23 days of the 30-day backfill never arrived. Each series now resolves its
  own chunked fetch range from its own resume point; disjoint ranges skip the
  other series' days (no extra load on AGL's BFF).
- **Zero-export days no longer stall the solar backfill.** A successfully
  fetched day whose feed-in intervals are all zero (cloudy day, or a solar
  system newer than the 30-day backfill floor) now writes a single
  zero-delta marker row so the generation resume point advances; previously
  a full chunk of such days was refetched forever and the period sensors
  never unlocked. Found by Codex review on the beta.2 PR.
- `tests/fixtures/solar_hourly_response.json` replaced with the tester's real
  full-day capture (48 slots, 11 non-zero export slots, mixed
  `normal`/`peak` feedIn types); reconciliation totals are locked in as
  regression tests.

---

## [0.4.0-beta.1] — 2026-07-05

> **Pre-release for community validation.** Solar generation support (#128)
> has not yet been confirmed against a live solar contract, and the
> `"pending"` interval filter (#126) awaits confirmation from an affected
> ToU account. Install via HACS with "Show beta versions" enabled.

### Added

- **Solar generation (feed-in) support** (#128). Contracts that report
  `hasSolar` in `/v3/overview` now also fetch the `ElectricitySolar` Hourly
  endpoint each cycle and write two new statistics series per contract:
  - `haggle:generation_<contract>` — exported kWh (`unit_class="energy"`), an
    Energy-dashboard **"Return to grid"** source
  - `haggle:generation_credit_<contract>` — AUD feed-in credit
  Values come from the response's `feedIn` block (outer `quantity`/`amount`,
  same schema as `consumption` — documented from a real capture provided on
  #128). Two new sensors, **Solar generation** (cumulative kWh) and **Solar
  feed-in credit** (cumulative AUD), register only on solar contracts;
  non-solar installs are unchanged. Backfill, trailing-rewindow self-healing,
  and the earliest-fetched-hour baseline rule all apply to the new series.
  If solar is added to a contract later, the integration detects the
  `hasSolar` flip on the next poll and reloads to add the sensors.

### Fixed

- **Parser now filters `consumption.type = "pending"` intervals (#126).** AGL
  returns `"pending"` (distinct from `"none"`) for 30-min and daily slots where
  the AEMO meter read exists in their system but has not yet been delivered to the
  BFF — confirmed via proxy trace. These slots carry non-zero `quantity`/`amount`
  values that look real but are preliminary estimates; letting them through caused
  phantom readings in the statistics that were never overwritten once the real AEMO
  value arrived (unlike the zero-on-zero placeholder case). Both
  `parse_interval_readings` and `parse_daily_readings` now skip `"pending"` the
  same way they skip `"none"`.

### Changed

- **Platform floor raised to Home Assistant 2026.7.0** (was 2026.6.3); `hacs.json`
  minimum bumped to match. Test harness pinned to
  `pytest-homeassistant-custom-component>=0.13.344,<0.13.345` (which pins
  `homeassistant==2026.7.0` and `pytest==9.0.3`); `ruff>=0.15.20`.
  `pytest` stays at `>=8.0` — `phcc==0.13.344` pins `pytest==9.0.3` exactly, so
  the Dependabot `pytest>=9.1.1` bump (#120) is unsatisfiable alongside phcc and
  is excluded from this rollup.
  `aiohttp` remains at `>=3.13.5` — HA still pins `aiohttp==3.13.5`, so
  Dependabot PR #106 (`aiohttp>=3.14.1`) is left open until HA moves upstream.
- **GitHub Actions rolled forward (all SHA-pinned)**:
  `actions/checkout` v7.0.0, `actions/setup-python` v6.3.0,
  `github/codeql-action` v4.36.3.

---

## [0.3.2] — 2026-06-24

### Fixed

- **Per-tariff cumulative-sum baseline could reset to 0 for a long-absent band
  (#114).** The trailing-rewindow baseline lookup searched only the
  `BACKFILL_DAYS` (30-day) window before the fetch cutoff. A ToU band absent for
  longer than that window but with older stored history resolved to a `0.0`
  baseline; if the band then reappeared inside the 7-day rewindow, its cumulative
  `sum` restarted from zero — a downward step that breaks the per-band
  `TOTAL_INCREASING` monotonicity for that one series. Baseline resolution now
  has a second, reach-back stage: any series with no rows in the normal window is
  looked up again from the start of recorded history, still bounded strictly
  *before* the fetch cutoff (never `get_last_statistics`, which would read a sum
  from inside the rewindow rows about to be rewritten). The cheap windowed lookup
  still resolves the normal and sparse-but-recent case in a single batched call;
  the reach-back fires only for genuinely missing series. The same hardening now
  also covers the aggregate baseline. Regression tests:
  `test_baseline_reaches_back_when_band_absent_from_window`,
  `test_baseline_no_reach_back_when_band_in_window`, and a per-hour partition
  assertion `test_per_tariff_states_partition_aggregate`.

### Changed

- **Platform floor raised to Home Assistant 2026.6.3** (was 2026.5.1);
  `hacs.json` minimum bumped to match, so HACS refuses to install on older HA.
  Test harness pinned to `pytest-homeassistant-custom-component>=0.13.339,<0.13.340`
  (which pins `homeassistant==2026.6.3`), and dev-tooling floor `ruff>=0.15.17`.
  `aiohttp` stays at `>=3.13.5`: HA pins `aiohttp==3.13.5` exactly, so the
  dependabot `aiohttp>=3.14.1` bump (#106) is unsatisfiable until HA moves it
  upstream — left open rather than merged.
- **GitHub Actions rolled forward (all SHA-pinned)**: `actions/checkout` v6.0.3,
  `astral-sh/setup-uv` v8.2.0, `codecov/codecov-action` v7.0.0,
  `github/codeql-action` v4.36.2, and `home-assistant/actions/hassfest`.

### Notes

- **#91 (clear external statistics on uninstall) — resolved as won't-implement,
  retain history.** Deleting the `haggle:*` recorder statistics on uninstall
  would silently and unrecoverably destroy the user's historical Energy-dashboard
  data. Orphaned statistics are harmless and the user can prune them on their own
  terms via **Developer Tools → Statistics**. `async_remove_entry` now documents
  the deliberate omission so it isn't "fixed" later.

---

## [0.3.1] — 2026-06-21

### Fixed

- **Phantom kWh spike in the cumulative sum at every local midnight.** The
  trailing rewindow looked up its cumulative-sum baseline at `fetch_start`
  UTC midnight, but AGL's `period=` query is interpreted in the contract's
  local timezone — the first interval of a day query lands at
  `(fetch_start - 1)T14:00Z` for an AEST account. The baseline therefore folded
  ~10 h of about-to-be-overwritten old sums in, and the new chain re-added
  those hours' deltas, producing a phantom `+N kWh` jump in the recorder `sum`
  column every local-midnight UTC row. The per-hour `state` was always correct,
  but the Energy dashboard plots hourly deltas as `sum[h] - sum[h-1]`, so the
  spike was visible there. `_import_intervals` now resolves the baseline — for
  the aggregate **and** every per-tariff ToU series — at the actual earliest
  fetched-interval hour, which is correct regardless of timezone or DST.
  Confirmed against the live recorder: 8 phantom spikes at `T14:00Z` of
  10–27 kWh, including in the ToU `consumption_normal` series. Regression test:
  `test_baseline_looked_up_at_earliest_fetched_hour`.
- **Recovery for existing installs.** New rows inside the current 7-day
  rewindow are rewritten with correct sums on the next poll. Older rows outside
  the rewindow keep their historical phantom — to clear them, delete the
  `haggle:*` rows from `statistics` / `statistics_short_term` and let the
  30-day backfill rebuild from scratch (same procedure as the v0.2.1 undercount
  recovery).

---

## [0.3.0] — 2026-06-20

**Stable release.** Promotes `0.3.0-beta.1` to stable after a multi-week live
soak. Headline feature: **Time-of-Use (ToU) tariff support** — per-tariff
consumption/cost statistics plus per-band rate sensors, with flat-rate contracts
unchanged. No code change vs `0.3.0-beta.1`; see the `[0.3.0-beta.1]` entry below
for the full breakdown.

> **Time-of-Use caveat.** ToU has not been validated against a real AGL ToU
> account — the maintainer's live contract is flat-rate, and the original
> requester (#82) did not return to test the beta. The flat-rate path is the
> soaked, stable path. On ToU contracts the consumption/cost **split** is driven
> by the documented per-interval `consumption.type` and is expected correct, but
> the per-band **rate sensors** use an unvalidated plan-text heuristic (#90) and
> may read `unavailable`; a sparse-band cumulative-sum edge case is tracked in #114.

Closes #82.

---

## [0.3.0-beta.1] — 2026-05-29

### Added

- **Time-of-Use (ToU) tariff support** (#82). On contracts whose AGL interval
  data is tagged with `peak`/`offpeak`/`shoulder` tariff types, the integration
  now writes a separate consumption + cost statistic per tariff band
  (`haggle:consumption_peak_<contract>`, `…_offpeak_…`, `…_shoulder_…`, plus a
  `…_normal_…`/anytime band so the parts sum back to the aggregate). Each is an
  independent Energy-dashboard source with `unit_class="energy"`.
  - Adds per-tariff unit-rate sensors (peak/off-peak/shoulder, `AUD/kWh`,
    `state_class=MEASUREMENT`), registered only on ToU contracts so flat-rate
    users see no empty sensors.
  - Flat-rate contracts are unchanged — only the existing aggregate
    `haggle:consumption_<contract>` / `cost_<contract>` series are written.
  - **Energy dashboard**: ToU users should add only the per-tariff consumption
    series (not the aggregate as well) to avoid double-counting; flat-rate users
    add only the aggregate.
  - Switching an existing flat-rate contract to a ToU plan: the per-tariff
    statistics appear automatically on the next poll; the integration schedules
    a one-off reload so the new per-tariff rate sensors register.

### Changed

- **Dev-tooling floor bumped**: `pytest-cov>=7.1.0` (was `>=5.0`); the
  lock already had 7.1.0 so no re-resolve needed.
- **Security patch**: `idna` 3.13 → 3.15 (uv.lock) — resolves CVE-2026-45409.
- **GitHub Actions pinned SHAs updated**: `codeql-action` v4.35.4 → v4.36.0,
  `codecov-action` v6.0.0 → v6.0.1, `home-assistant/actions/hassfest`
  SHA updated to `868e6cb4`.

### Fixed

- Corrected stale code comments that named `consumption.values.quantity` (inner,
  DPI-scaled) as the kWh source of truth in `models.py` and `client.py`; the
  metered value is `consumption.quantity` (outer), as the parser already used.

Closes #73, #75, #77, #83, #85.

---

## [0.2.3] — 2026-05-15

### Changed

- **Platform floor bumped to Home Assistant 2026.5.1** (was `2026.5.0`).
  Tracks the `pytest-homeassistant-custom-component` test harness
  (`0.13.330` pins `homeassistant==2026.5.1`); the test fixture and the
  runtime floor stay aligned. `hacs.json:homeassistant` updated to match.
- **Dev-tooling floors bumped**: `ruff>=0.15.13` (was `>=0.15.12`),
  `mypy>=2.1.0` (was `>=2.0.0`). Pipeline runs clean against the existing
  codebase.
- **`uv.lock` regenerated** for the patch bumps above.

Closes #61, #62, #63.

### Repo

- `.gitignore` adds `security/` so locally-staged security review scratch
  doesn't get accidentally committed.

---

## [0.2.2] — 2026-05-15

### Changed

- **Platform floor bumped to Home Assistant 2026.5.0.** Tracks the
  `pytest-homeassistant-custom-component` test harness (`0.13.329` pins
  `homeassistant==2026.5.0`); the test fixture and the runtime floor stay
  aligned. The 13-CVE closure from 2026.4 (`aiohttp==3.13.5`,
  `cryptography>=46.0.6`, `orjson>=3.11.6`) is preserved at-or-above on
  2026.5.0. `hacs.json:homeassistant` updated to match.
- **Runtime dep floor `aiohttp>=3.13.5`** (was `>=3.13.4`). Matches what HA
  2026.4+ already bundles; eliminates a stale floor.
- **Dev-tooling floors bumped**: `ruff>=0.15.12`, `mypy>=2.0.0`,
  `pre-commit>=4.6.0`. No new diagnostics surfaced — ruff and mypy both run
  clean against the existing codebase.
- **CI action SHAs rolled forward**: `astral-sh/setup-uv@v7.6.0 → v8.1.0`,
  `github/codeql-action@v4.35.3 → v4.35.4`. Both pinned to 40-char SHAs.
- **`uv.lock` regenerated** — notable transitive bumps: `urllib3 2.6.3 →
  2.7.0`, `sqlalchemy 2.0.41 → 2.0.49`, `pyopenssl 26.0.0 → 26.1.0`.

Closes #60, #61, #62, #63, #64, #65, #66, #69 (rolled up into one release
so the test harness and runtime floor land together).

---

## [0.2.1] — 2026-05-15

### Fixed

- **Energy meter undercounting (severe).** The parser was reading kWh from
  `consumption.values.quantity` (a DPI/chart-scaled helper), not
  `consumption.quantity` (the real meter read). This undercounted the Energy
  dashboard by 4-73% per day with no consistent ratio. Confirmed against the
  AGL portal "MyUsageData" CSV across 11 mitm captures, 2026-05-12. Cost
  values were already read correctly from `consumption.amount` and are
  unaffected. Anyone running v0.1.0 / v0.2.0-beta.{1,2,3} should wipe the
  `haggle:*` rows from `statistics` / `statistics_short_term` after upgrading
  and let the 30-day backfill rebuild from scratch.
- **AGL placeholder days no longer create phantom zero rows.** On days where
  AEMO hasn't delivered the meter reads yet, AGL returns intervals with a
  non-`none` type but `quantity == 0 && amount == 0`. The parser now drops
  these instead of writing 24 zero-kWh hourly rows that the resume logic
  would never re-check.
- **Trailing rewindow self-heals AGL backfills.** Every poll now re-fetches
  the last `REWINDOW_DAYS` (default 7) so a slot first returned as a
  placeholder is overwritten when AGL has the real read. The cumulative-sum
  baseline for the rewindow is looked up via `statistics_during_period`
  (sum at the hour right before fetch_start UTC midnight), not the latest
  stored sum.

---

## [0.2.0] — 2026-05-05

**Stable release.** Promotes `v0.2.0-beta.3` to stable after a clean 24 h+ live soak.
No code change vs `0.2.0-beta.3`.

Cumulative changes since `v0.1.0` (yanked) — see `[0.2.0-beta.1]`..`[0.2.0-beta.3]`
for the per-beta breakdown:

- Trust-On-First-Use TLS pinning for both AGL hosts.
- 14-finding SAST sweep + supply-chain hardening (SHA-pinned actions, scoped
  permissions, build-provenance attestation).
- Platform floor bumped to Python 3.14.2 / HA 2026.4.4 — closes 13 inherited CVEs.
- DeviceInfo no longer claims AGL Energy authorship.
- Six issue-tail fixes (#34, #35, #37, #38, #49, #50) + monetary state_class
  follow-up (PR #59).
- LICENSE canonicalized so GitHub auto-detects `Apache-2.0`.

---

## [0.2.0-beta.3] — 2026-05-05

### Fixed
- **Clear `state_class_removed` Repairs on `unit_rate` / `supply_charge`.**
  v0.2.0-beta.2 dropped `state_class=MEASUREMENT` from these to fix the
  inverse warning (#49), but that triggered HA's `state_class_removed`
  Repair on installs that had previously recorded stats. Root cause was
  using `device_class=MONETARY` for unit prices in the first place —
  MONETARY is for cumulative amounts, not rates. Drop MONETARY from
  `unit_rate` / `supply_charge` and restore `state_class=MEASUREMENT`;
  HA's price-tracking integrations (Nordpool, Tibber) use the same
  pattern. `bill_projection` keeps MONETARY without `state_class` —
  it's a forecast total, not a rate. Repairs banner clears on the next
  coordinator update.

---

## [0.2.0-beta.2] — 2026-05-04

### Fixed
- **GitHub now detects the license as Apache-2.0.** `LICENSE` was
  byte-for-byte the canonical Apache-2.0 template *except* the appendix
  placeholders had been filled in (`2026 Naanya Biz`) and the closing
  paragraph re-wrapped, which is enough to flip GitHub's `licensee`
  matcher to `NOASSERTION`. Replaced `LICENSE` with the verbatim
  template from https://www.apache.org/licenses/LICENSE-2.0.txt and
  moved the copyright attribution to a sibling `NOTICE` file (Apache-2.0
  § 4(d)). Verify with
  `gh api repos/NaanyaBiz/haggle/license --jq '.license.spdx_id'` →
  `Apache-2.0`. Closes #54.
- **MONETARY sensors no longer log a state-class warning on every poll.**
  HA rejects `state_class=MEASUREMENT` on `device_class=MONETARY`; drop
  the invalid `state_class` from `bill_projection`, `unit_rate`, and
  `supply_charge`. Closes #49.
- **Backfill loop now sleeps between per-day fetches and halts on 429.**
  First-install backfill no longer fires up to 7 GETs in <1 s; on
  rate-limit the loop stops so the next 24 h cycle resumes from the
  gap rather than silently dropping post-429 days. Closes #34.
- **Removing the integration now purges its entity-registry rows.**
  `async_remove_entry` walks the registry and deletes orphans for the
  config entry, preventing `_2`-suffixed re-installs. Closes #50.

### Changed
- **Coordinator overlaps recorder reads.** Both `get_last_statistics`
  lookups (consumption + cost) now run via `asyncio.gather`, halving
  the wall time of the resume-point computation. Closes #35.
- **Code cleanup.** Drop unused `SCAN_INTERVAL_DAILY`,
  `SCAN_INTERVAL_PLAN`, `TOKEN_REFRESH_MARGIN_SECONDS` constants; drop
  the `__all__` re-export block from `agl/client.py`; correct the
  `agl/__init__.py` docstring to reference `agl-api-explorer`. Remove
  defensive `NotImplementedError` catches in the coordinator and the
  dict-fallback branch in `HaggleEnergySensor.native_value` — neither
  path is reachable from production code. Closes #37, #38.

---

## [0.2.0-beta.1] — 2026-05-04

First post-flip beta. Major work since v0.1.0:

- **Trust-On-First-Use TLS pinning** for both AGL hosts (closes AP-1
  from `security/2026-05-02T04-43Z/`).
- **14-finding SAST sweep** addressing AP-2/AP-4/AP-6 chains.
- **Supply-chain hardening**: all GitHub Actions SHA-pinned;
  workflow-level `permissions: read-all`; HACS validation gate
  un-suppressed; release.yml hardened against changelog interpolation.
- **Repo posture files**: `SECURITY.md`, `CODEOWNERS`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, weekly CodeQL scan,
  build-provenance attestations on release.
- **Platform bump** to Python 3.14.2 / HA 2026.4.4 — closes the 13
  outstanding aiohttp/cryptography/orjson CVEs at the source.
- **Branding correction**: `DeviceInfo.manufacturer` no longer
  claims AGL Energy authorship.

This is the first version intended for HACS submission. Marked beta
until it has soak-time on independent installs.

### Changed
- **Platform floor bumped to Python 3.14.2 + Home Assistant 2026.4.4.**
  HA 2026.4 ships `aiohttp==3.13.5` (closes 9 CVEs in SCA-M01..M04 +
  SCA-L01..L06), `cryptography>=46.0.7` (closes SCA-M05 + SCA-L08), and
  `orjson>=3.11.7` (closes SCA-H02). HA 2026.4 itself requires Python
  3.14.2, so the integration's `requires-python` and CI matrix follow.
  Net effect: 13 of the 21 Dependabot alerts auto-close as `state=fixed`
  on the next scan; the remaining 8 were already dismissed as not-used
  on 2026-05-03. Bumps `hacs.json:homeassistant` to `2026.4.4`, so HACS
  refuses to install on older HA. CHANGELOG previously deferred this to
  v0.2.x; the live install verified clean on 2026-05-03 so we bring it
  forward.
- **`pyproject.toml`** — `requires-python>=3.14.2`, `homeassistant>=2026.4.4`,
  `aiohttp>=3.13.4`, `pytest-homeassistant-custom-component>=0.13.325,<0.13.326`.
  Ruff `target-version = "py314"`; mypy `python_version = "3.14"`.
- **`.github/workflows/ci.yml`** — matrix `python-version: ["3.14"]`.
- **Three `test_config_flow` tests** now patch `async_setup_entry` to a
  no-op stub. pytest-HA 0.13.325 is stricter about socket use during
  `flow.CREATE_ENTRY` teardown; previously the coordinator's first
  refresh leaked through. Patch is harmless — the tests assert
  flow-level state (entry data shape, PKCE secret zeroing), not setup
  behaviour.

### Fixed
- **`DeviceInfo` no longer claims AGL authorship.** `sensor.py` previously
  set `manufacturer="AGL Australia"` and `model="AGL Energy API"`, which
  HA's "Service info" card rendered as `AGL Energy API by AGL Australia`
  — implying the integration was an official AGL product. It is not.
  Updated to `manufacturer="Haggle"` and
  `model="AGL smart-meter (unofficial integration)"`. Added a regression
  test that asserts neither field contains the AGL name. README and
  info.md already carry the "unofficial / not affiliated with AGL"
  disclaimer; this fix brings the in-app HA UI into line with the docs.
- **TOFU SPKI pinning was silently broken on every install.** The first
  attempt (#45) extracted the SPKI from `resp.connection.transport` after
  the response context entered, but aiohttp had already released the
  Connection back to its pool — `resp.connection` was `None` and the
  capture silently degraded to no-pin (verified empty in live `entry.data`
  on 2026-05-03). Replaced with a `HagglePinningConnector` (`TCPConnector`
  subclass) that overrides `_wrap_create_connection` to capture the SPKI
  during the TLS handshake itself, before any response object exists.
  The connector exposes `observed[host]` and an optional
  `on_new_connection(host, spki)` callback for warn-on-mismatch validation.
  New regression tests stand up a real local TLS server with a known cert
  and assert the connector populates `observed` correctly — these would
  have caught the lifecycle bug. The integration owns its own
  `aiohttp.ClientSession(connector=HagglePinningConnector)` rather than
  HA's shared session (HA's shared connector cannot be subclassed); the
  session is closed in `async_unload_entry`.

### Added
- **`SECURITY.md`** at the repo root — disclosure path
  (`security@naanya.biz` and GitHub private security advisories), threat
  model summary including the TOFU pinning behaviour, scope, and
  coordinated-disclosure expectations.
- **`CONTRIBUTING.md`** — dev loop, commit conventions, branch and PR
  workflow, and a pointer to `AGENTS.md` for AGL-specific contribution
  steps.
- **`CODE_OF_CONDUCT.md`** — Contributor Covenant 2.1 with private
  reporting via `security@naanya.biz`.
- **`.github/CODEOWNERS`** — `@naanyabiz` owns everything; auto-routes
  PR review requests once branch protection requires them.
- **`.github/workflows/codeql.yml`** — Python CodeQL scan on every push
  to `main`, every PR, and weekly on Monday. Uses the
  `security-extended,security-and-quality` query packs.
- **Build-provenance attestation in `release.yml`** — every tag-triggered
  GitHub Release now ships a Sigstore-rooted attestation against the
  `custom_components/haggle/` source tree via
  `actions/attest-build-provenance`. Verify with `gh attestation verify`.
- **AGENTS.md "GitHub Issues Workflow" section** documenting when to
  open an issue vs PR, the `Closes #N` convention, and that issues are
  the canonical place for "do this next round" items (closes #22).

### Added (previous)
- **Trust-On-First-Use TLS certificate pinning** for `secure.agl.com.au` and
  `api.platform.agl.com.au`. The SHA-256 SPKI hash of each AGL host's leaf
  certificate is captured during the initial PKCE config flow and persisted
  to the config entry; every subsequent token refresh and BFF request is
  observed and compared. Mismatches surface as a HA persistent notification
  (`haggle_pin_mismatch_<host>`) plus a WARNING log — they do **not** block
  the request, so a legitimate AGL cert rotation cannot brick HACS users.
  Re-pin via the standard Reconfigure flow on the integration card. New
  module `custom_components/haggle/agl/pinning.py`. Closes AP-1 from
  `security/2026-05-02T04-43Z/`.

### Fixed
- **`coordinator.py` uses UTC for the fetch range** instead of the OS local
  date. AGL `dateTime` slots are UTC; `date.today()` on a non-UTC HA host
  could fetch tomorrow's empty data or skip yesterday entirely around
  midnight. (#31)
- **`config_flow._exchange_code` and `_fetch_contracts` use HA's shared
  aiohttp client** (`async_get_clientsession(hass)`) instead of creating a
  throwaway `aiohttp.ClientSession()` per call. Inherits HA's TCP connector
  pool. (#28)
- **`__init__.py::async_setup_entry` uses `async_create_clientsession(hass)`**;
  the manual `session.close()` in `async_unload_entry` is gone — HA owns the
  session lifecycle now. The `session` field on `HaggleRuntimeData` is
  removed since callers no longer need to reach for it. (#29)

### Removed
- **Three never-called `AglClient` methods**: `async_get_servicehub`,
  `async_get_usage_daily`, `async_close`. (#32)
- **`CONF_ACCESS_TOKEN` and `CONF_ACCESS_TOKEN_EXPIRY` constants** from
  `const.py` and the empty-string/zero values that were being written into
  `entry.data` on creation. AGENTS.md already prohibited persisting access
  tokens; the code was a footgun for future contributors. (#26)
- **`beautifulsoup4` runtime dependency**. Zero call sites in
  `custom_components/`; was a dead dep that every HACS installer downloaded for
  no reason. Removed from `manifest.json` and `pyproject.toml` (also drops
  `types-beautifulsoup4` from dev deps and the `soupsieve` / typed-stub
  transitives from `uv.lock`). Resolves SCA noise plus HACS-posture B1/B2
  from `security/2026-05-02T04-43Z/`.

### Security
- **PKCE verifier and challenge are zeroed after a successful exchange**.
  The flow object can persist in memory across multi-step retries; a stale
  one-shot verifier is one less secret to leak. (#27)
- **Note on `aiohttp` CVE coverage**: the 9 CVEs against `aiohttp==3.13.3`
  flagged in the security review (SCA-M01, SCA-M04, SCA-L01..L06) are fixed in
  `aiohttp>=3.13.4`, which Home Assistant bundles starting with `2026.4.0`. HA
  `2026.4.0` also bumps the Python floor to `3.14.2`, so a hard `aiohttp>=3.13.4`
  pin would force a Python platform bump for every HACS user. Deferred to a
  v0.2.x release that promotes the platform floor deliberately. Users on
  HA `2026.4.0+` already receive the patched runtime.
- **Pin all GitHub Actions to commit SHAs** across `ci.yml`, `hacs.yml`,
  `hassfest.yml`, `release.yml`. Closes the supply-chain branch-poisoning vector
  on `hacs/action@main` and `home-assistant/actions/hassfest@master`.
- **Add `permissions: read-all` to `ci.yml`, `hacs.yml`, `hassfest.yml`**.
  `release.yml` retains its job-level `contents: write`.
- **Remove `continue-on-error: true` from the HACS validation step**. The
  underlying validation passes (`All (8) checks passed` confirmed against
  `main`); the suppression flag was hiding real failures from CI.
- **Switch `release.yml` to `body_path:`** instead of interpolating
  `${{ steps.changelog.outputs.body }}` directly into the release body, removing
  the shell-context injection vector.
- **Hash refresh-token before using it as a fallback `unique_id`** in the config
  flow. The HA entity registry is plaintext JSON on disk; previously the first
  16 chars of the live OAuth2 refresh token landed there when contracts were
  unavailable at setup time. Now uses `sha256(refresh_token)[:16]`. (SAST-001)
- **Use the return value of `AglAuth.async_force_refresh`** on the 401-retry
  path in `AglClient._get` instead of reading `_auth._token_set.access_token`
  via private-attribute access. The retry path no longer silently masks auth
  failures. (SAST-002)
- **Redact AGL/Auth0 response bodies from exceptions** that propagate to
  `ConfigEntryAuthFailed` / `UpdateFailed`. Bodies and PII-bearing URLs now go
  to `_LOGGER.debug` only — they no longer surface in HA Persistent
  Notifications or the default `home-assistant.log`. Affects token refresh,
  the `_get` error path, and the config-flow `_fetch_contracts` overview call.
  (SAST-003, SAST-004)
- **Consolidate the `auth0-client` SDK identity blob** to a single
  `AGL_AUTH0_CLIENT` constant in `const.py`. Previously the same JSON shape was
  base64-encoded twice with different field ordering — once in `const.py`,
  once in `agl/client.py` — risking inconsistent headers per call site.
  (SAST-006)
- **Allowlist plan-rate fields** in `parse_plan` instead of forwarding the
  raw API rate dict via `dict(rate)`. Only `kind`, `type`, `title`, `price`
  propagate into `PlanRates.unit_rates` — closes the open-schema vector that
  a MITM AGL response could exploit to inject keys into coordinator state.
  (SAST-007)
- **Bound numeric API values** with a shared `_safe_float()` helper in
  `agl/parser.py` and `coordinator.py`. Non-finite (`inf`, `nan`) and negative
  values clamp to `0.0` with a warning so adversarial AGL responses can no
  longer poison the recorder via `async_add_external_statistics`. (SAST-008)
- **Trigger reauth on persist failure**: if `async_update_entry` raises while
  saving a rotated refresh token, `__init__.py::_persist_refresh_token` now
  calls `entry.async_start_reauth(hass)` immediately instead of silently
  continuing in split-brain state. (SAST-009)

---

## [0.1.0] [YANKED] — 2026-05-02

> **Withdrawn 2026-05-04.** The GitHub Release for `v0.1.0` was deleted from
> https://github.com/NaanyaBiz/haggle/releases. The git tag `v0.1.0`
> (`fb25813`) is preserved for history. Reasons:
>
> - Trust-On-First-Use TLS pinning was silently a no-op — `entry.data` was
>   verified empty on the live install on 2026-05-03 — meaning installs
>   accepted any AGL-presented certificate without comparison. Fixed in
>   v0.2.0-beta.1.
> - `DeviceInfo` rendered as `AGL Energy API by AGL Australia` in HA's
>   Service info card, falsely implying official AGL authorship. Fixed in
>   v0.2.0-beta.1.
> - 13 unpatched CVEs against the bundled `aiohttp` / `cryptography` /
>   `orjson` (all gated on the HA platform floor at the time). Closed by
>   the Python 3.14.2 / HA 2026.4.4 bump in v0.2.0-beta.1.
>
> Anyone running v0.1.0 should reinstall from v0.2.0-beta.1 (HACS users:
> enable "Show beta versions" or wait for v0.2.0 stable).

### Fixed
- **`_fetch_contracts` token-type bug**: config flow was passing the short-lived
  `access_token` to `AglAuth` as a `refresh_token`; Auth0 rejected it, contracts
  fell back to empty, and every API URL became `.../Electricity/?...` (HTTP 404).
  `_fetch_contracts` now makes a direct `aiohttp` GET to `/v3/overview` with a
  `Authorization: Bearer` header — no `AglAuth` involved.
- **HTTP 500 on all Hourly/Daily usage endpoints**: AGL's BFF requires three
  headers (`Accept-Features`, `Client-Device`, `Accept-Language`) and a `scaling`
  query parameter that were missing from `AglClient._default_headers` and the
  usage URL builders. Captured from mitmproxy iOS session 2026-05-01.
- **`consumption_period_kwh` always 0**: `parse_bill_period` hardcoded
  `consumption_kwh=0.0`; now parses `usage.quantity` ("259 kWh" → `259.0`).
- **Consumption statistic invisible in Energy dashboard**: `StatisticMetaData`
  was registered with `unit_class=None`; HA's Energy picker filters on
  `unit_class="energy"`. Fixed — statistic now appears in the grid-consumption
  source dropdown.

### Changed
- **Chunked throttled backfill**: first-install backfill no longer fires 30 API
  calls at startup. `_async_setup` is now a no-op; `_fetch_and_import` fetches
  at most `BACKFILL_CHUNK_DAYS=7` days per 24 h poll cycle, working backwards
  from the most-recently-imported date.
- **Smart endpoint selection**: days inside the current billing period use
  `Current/Hourly`; older days use `Previous/Hourly`. Selection is driven by
  `bill_period.start` returned by the usage-summary endpoint.

### Added
- **Consumption cost sensor** (`consumption_period_cost_aud`): AUD cost for the
  current billing period, sourced from `usage.amount` in the AGL summary response.
- **`AGL_ACCEPT_FEATURES`, `AGL_CLIENT_DEVICE`, `AGL_SCALING`** constants
  (captured from iOS app 8.38.0-531 via mitmproxy).
- **`BACKFILL_CHUNK_DAYS`** constant (default 7) controlling how many days are
  fetched per poll cycle.
- **config_flow.py**: `_fetch_contracts` failure no longer silently creates a
  broken entry with empty contract/account numbers. Now shows a `cannot_connect`
  error on the `select_contract` step so the user can retry.
- **config_flow.py**: INFO log after `_async_create_entry` records account +
  contract numbers.
- **__init__.py**: INFO log at top of `async_setup_entry` records contract
  number. `_persist_refresh_token` wrapped in try/except — ERROR on failure,
  DEBUG (token length only) on success.
- **coordinator.py**: INFO log at start of 30-day backfill (contract + day
  count); INFO at end (interval count, day count, both statistic IDs). INFO
  log on incremental path (date range + interval count).
- **agl/client.py**: Token-refresh log promoted DEBUG → INFO; now includes
  `expires_at` ISO timestamp.
- **info.md / README.md**: Install steps rewritten to document the actual PKCE
  flow (authorize URL → browser login → paste callback URL) instead of the
  stale email+OTP flow.
- **Three new review subagents** under `.claude/agents/`: `code-quality-reviewer`,
  `security-reviewer`, `async-performance-reviewer`. Auto-trigger on edits to
  the relevant areas of `custom_components/haggle/`; complement the existing
  five domain agents.

### Removed
- **`consumption_today` sensor**: AGL data has a 24–48 h AEMO feed lag; this
  sensor was always 0 and only caused confusion.

---

## [0.0.1-dev] — 2026-05-01 (Sprint 1 — not yet formally released)

### Added
- **PKCE config flow** (`config_flow.py`): generates Auth0 authorize URL with
  PKCE S256 challenge; user opens URL in their real browser (handles Akamai
  bot-protection + MFA), pastes callback URL back; integration extracts `code`
  and exchanges for `refresh_token` + `access_token`. Multiple-contract
  households get a contract-selection step.
- **AGL client** (`agl/client.py`): `AglAuth` decodes JWT `exp`, refreshes
  access token ≤2 min before expiry, and persists the rotated `refresh_token`
  via callback. `AglClient` implements all data endpoints: overview, Current
  and Previous hourly intervals, daily series, usage summary, plan/rates.
- **Typed models** (`agl/models.py`): `TokenSet`, `Contract`, `IntervalReading`,
  `DailyReading`, `BillPeriod`, `PlanRates`.
- **Parser** (`agl/parser.py`): JSON → typed dataclasses. Uses
  `consumption.values.quantity` as kWh source-of-truth (not the UI-rounded
  `consumption.quantity`). Filters `type=none` (future/unavailable) intervals.
- **Coordinator statistics import** (`coordinator.py`): On first install,
  backfills 30 days from `/Previous/Hourly`. On each 24 h poll, fetches missing
  days via `get_last_statistics` resume point and imports via
  `async_add_external_statistics`. 30-min intervals aggregated to hourly UTC
  buckets. Statistic IDs: `haggle:consumption_<contract>` (kWh) and
  `haggle:cost_<contract>` (AUD). Running `sum` threads through all rows for
  idempotent re-import.
- **Live sensors** (`sensor.py`): `consumption_period_kwh`,
  `consumption_period_cost_aud`, `bill_projection_aud`, `unit_rate_aud_per_kwh`,
  `supply_charge_aud_per_day`, `latest_cumulative_kwh`.
- **Test suite**: 63 tests — `test_init.py`, `test_config_flow.py`,
  `test_agl_client.py`, `test_parser.py` (22 tests),
  `test_coordinator_statistics.py` (26 tests). JSON fixtures under `tests/fixtures/`.
- **`/pr` slash command** with worktree-aware merge documentation.

### Infrastructure (pre-Sprint 1)
- Initial repo scaffolding: license, README, `.gitignore`, `.gitattributes`.
- Python tooling: `pyproject.toml` with ruff (strict), mypy (strict), pytest;
  `pre-commit` with ruff/mypy/gitleaks/commitlint; devcontainer.
- AI/Claude infrastructure: `.claude/` with hooks, 5 subagents, slash commands;
  `AGENTS.md` (canonical) with `CLAUDE.md` symlink; `scripts/wt` worktree helper.
- CI: hassfest, hacs validation, ruff/mypy/pytest matrix, release workflow on tag.
- HACS metadata: `hacs.json`, `info.md`, placeholder `brand/icon.png`.

### Notes
- Repo remains private until a live install against a real AGL account is verified.
  Flip to public for HACS submission.
