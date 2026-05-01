# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (Phase A — pre-flight logging)
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

### Targets for next sprint
- End-to-end live install test against a real AGL account.
- Solar/feed-in sensor (needs a solar-customer mitmproxy capture).
- ToU (time-of-use) rate display — `rate_type` is already on each `IntervalReading`.

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
