# AGENTS.md — Haggle Integration Guide

> **One-liner**: `haggle` is a Home Assistant custom integration that pulls AGL Australia
> smart-meter interval data from AGL's undocumented REST API and feeds it into HA's
> Energy dashboard via `import_statistics()`.

This file is the canonical documentation for both human contributors and AI agents.
`CLAUDE.md` is a symlink to this file.

---

## Dev Loop

```bash
# Install deps (once, or after pyproject.toml changes)
uv sync

# Run tests
uv run pytest

# Lint + format
uv run ruff check --fix custom_components/ tests/
uv run ruff format custom_components/ tests/

# Type-check
uv run mypy custom_components/haggle

# Validate manifest
python scripts/validate_manifest.py custom_components/haggle/manifest.json

# Run all pre-commit hooks
uv run pre-commit run --all-files

# Hassfest (Docker required)
docker run --rm \
  -v "$(pwd)/custom_components:/github/workspace/custom_components:ro" \
  ghcr.io/home-assistant/home-assistant:dev \
  python -m script.hassfest --action validate \
  --integration-path /github/workspace/custom_components/haggle
```

---

## Repo Map

```
custom_components/haggle/
├── __init__.py          # async_setup_entry / async_unload_entry + HaggleRuntimeData
├── manifest.json        # HACS/HA metadata; hassfest validates this
├── const.py             # all constants — DOMAIN, API hosts, config-entry keys, data keys
├── config_flow.py       # ConfigFlow: user (refresh token paste) → select_contract
├── coordinator.py       # HaggleCoordinator(DataUpdateCoordinator[HaggleData])
├── sensor.py            # 6 SensorEntityDescription entries; HaggleEnergySensor
├── agl/
│   ├── __init__.py
│   ├── client.py        # AglAuth (token rotation) + AglClient (HTTP methods)
│   └── parser.py        # JSON → typed dataclasses
├── strings.json         # translatable config-flow strings
└── translations/en.json # English strings (must mirror strings.json)

tests/
├── conftest.py          # _auto_enable_custom_integrations fixture
├── test_init.py         # setup/unload smoke tests
└── test_config_flow.py  # config-flow step navigation

scripts/
├── wt                   # bash worktree helper (new / list / rm)
└── validate_manifest.py # used by the validate-manifest Claude hook

.claude/
├── settings.json        # committed hooks config
├── agents/              # 5 subagent definitions
└── commands/            # 4 slash commands
```

---

## Subagent Triggers

| Agent | File | Trigger condition |
|---|---|---|
| `ha-integration-architect` | `.claude/agents/ha-integration-architect.md` | Edits to `__init__.py`, `config_flow.py`, `coordinator.py`, `sensor.py`; HA-pattern questions |
| `agl-api-explorer` | `.claude/agents/agl-api-explorer.md` | Any work in `agl/`; new AGL endpoints; raw HTTP questions |
| `energy-domain-expert` | `.claude/agents/energy-domain-expert.md` | `state_class`, `device_class`, `unit_of_measurement` changes; `import_statistics()` usage |
| `ha-test-writer` | `.claude/agents/ha-test-writer.md` | After every change in `custom_components/haggle/`; proactively |
| `release-manager` | `.claude/agents/release-manager.md` | Only via `/release` command |

---

## Slash Commands

| Command | Usage | What it does |
|---|---|---|
| `/new-entity` | `/new-entity <key> <translation_key> <device_class> <state_class> <unit>` | Scaffolds sensor entity + test |
| `/wt` | `/wt new <branch>` \| `/wt list` \| `/wt rm <branch>` | Manages sibling git worktrees |
| `/release` | `/release 0.2.0` | Cuts a semver release via `release-manager` |
| `/hassfest` | `/hassfest` | Validates integration against hassfest rules |

---

## Worktree Workflow

Main worktree (`/Users/dave/projects/haggle/`) is always on `main`. Feature
work happens in sibling worktrees at `/Users/dave/projects/haggle.wt/<branch>/`.
Never commit directly to `main` from a feature worktree — always open a PR.

```bash
# Create a feature worktree
./scripts/wt new feat/agl-login

# Work in the new session:
# → open Claude Code at ../haggle.wt/feat-agl-login/

# Remove when done (refuses if dirty)
./scripts/wt rm feat/agl-login
```

Each worktree shares `.venv` and `.claude/settings.local.json` via symlink.

---

## AGL API — Key Facts

### Authentication (Auth0)

- **Auth host**: `https://secure.agl.com.au`
- **Token endpoint**: `POST /oauth/token`
- **Grant**: `refresh_token`
- **client_id**: `2mDkNcC8gkDLL7FTT1ZxF5rrQHrLTHL3` (iOS app — captured 2026-04-30)
- **Required headers**: `Client-Flavor: app.iOS.public.8.38.0-531`
- **Access token**: JWT (RS256), `exp` ≈ 24 h. Decode `exp`; refresh 5 min early.
- **CRITICAL — token rotation**: Auth0 **rotates** the refresh token on every
  exchange. The integration MUST persist the new refresh token via
  `_persist_refresh_token` callback after every exchange or it will lock
  itself out on the next restart.

### Data API

- **Base**: `https://api.platform.agl.com.au`
- **Required header**: `Client-Flavor: app.iOS.public.8.38.0-531`
- **Contract discovery**: `GET /mobile/bff/api/v3/overview`
  - Key fields: `accounts[].accountNumber`, `accounts[].contracts[].contractNumber`
  - `contractNumber` ≠ `accountNumber` — use `contractNumber` in all data paths
- **30-min interval data** (despite "Hourly" in the path):
  `GET /mobile/bff/api/v2/usage/smart/Electricity/{contractNumber}/Current/Hourly?period=YYYY-MM-DD_YYYY-MM-DD`
- **kWh source of truth**: `consumption.values.quantity` — NOT `consumption.quantity`
  (which is UI-rounded) and NOT `consumption.values.amount` (same value but semantically cost)
- **`dateTime` field**: slot start, in **UTC**. Convert to local for display.
- **`consumption.type`**: `normal` | `peak` | `offpeak` | `shoulder` | `none`
  (filter out `none` — future-dated or unavailable intervals)

### Polling Cadence

| Data | Interval | Reason |
|---|---|---|
| 30-min intervals | 24 h | AGL data is delayed 24-48 h (AEMO feed lag) |
| Daily series | 6 h | Picks up newly available days |
| Plan / overview | 7 days | Rarely changes |
| Token refresh | Just-in-time (< 5 min to `exp`) | |

**Do not poll for today's hourly data** — it will be empty. Fetch *yesterday*.

### Previous Bill Period

```
GET /mobile/bff/api/v2/usage/smart/Electricity/{contractNumber}/Previous/Hourly?period=...
```

Use on first install to backfill 30 days of history (one day at a time, ~1 req/s).

### Plan / Rates

```
GET /mobile/bff/api/v2/plan/energy/{contractNumber}
```

Returns `gstInclusiveRates` list with `c/kWh` and `c/day` entries. Supply charge
is a `c/day` entry with `title` containing "Supply charge".

---

## Energy Dashboard Contract

The HA Energy dashboard requires:
- `device_class = ENERGY`, `state_class = TOTAL_INCREASING`, `native_unit_of_measurement = kWh`
- Historical data MUST be fed via `recorder.import_statistics()` (not live state updates).
  AGL data is always historical — `import_statistics()` attributes it to the interval it
  actually occurred in, not "now". Skipping this means the Energy dashboard shows a spike
  at poll time, not a smooth historical chart.
- Statistic ID format: `haggle:{entity_id_suffix}` (e.g. `haggle:electricity_consumption`).
- Each import call should be idempotent: use `last_stats` to avoid double-counting.

---

## What NOT to Do

- **No `requests`** — always `aiohttp`. Blocking I/O in the event loop will freeze HA.
- **No blocking I/O in the coordinator** — `_async_update_data` must be fully async.
- **No OTP flow** — auth is Auth0 refresh token, not portal scraping with OTP.
- **No hardcoded contract numbers** — they come from `/v3/overview` at config time.
- **No polling faster than 24 h for interval data** — AGL won't have newer data.
- **Don't store `access_token` in `entry.data`** long-term — it's transient (24 h).
  Persist only `refresh_token` to `entry.data`; keep `access_token` in memory only.
- **Don't use `async_add_executor_job`** for AGL API calls — they're already async.
- **No committing directly to `main`** — the `guard-main-branch` hook blocks it.
  Use a feature branch + PR.

---

## Commit Conventions

Conventional Commits format is enforced by `commitlint` pre-commit hook:

```
feat: add daily consumption sensor
fix: handle missing consumption.values.quantity
chore(release): v0.2.0
ci: add hacs workflow
```

Every commit MUST include the `Co-Authored-By: Claude` trailer. The
`require-claude-coauthor` pre-commit hook enforces this. Example:

```bash
git commit -m "feat: implement token rotation persistence

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Provenance

This codebase was generated by AI (Claude, Anthropic) and reviewed by the human
maintainer (@davosparent). All commits carry `Co-Authored-By: Claude` trailers.
The AGL API was reverse-engineered from a real iOS app session via mitmproxy —
see `~/scratch/aglreversing/AGL-API-FINDINGS.md` for the full capture notes.
No proprietary AGL code is included; the integration uses only publicly observable
HTTP traffic from a legitimate AGL customer account.
