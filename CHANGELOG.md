# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
