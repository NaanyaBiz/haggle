# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_No unreleased changes yet._

---

## [0.1.0] — 2026-05-02

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
