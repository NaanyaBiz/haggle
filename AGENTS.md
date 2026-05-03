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

# Hassfest — easiest via CI (push a branch + open PR)
# Or use the dedicated image locally:
docker run --rm \
  -v "$(pwd)/custom_components:/github/workspace/custom_components:ro" \
  ghcr.io/home-assistant/hassfest \
  --integration-path /github/workspace/custom_components/haggle
```

---

## Repo Map

```
custom_components/haggle/
├── __init__.py          # async_setup_entry / async_unload_entry + HaggleRuntimeData
├── manifest.json        # HACS/HA metadata; hassfest validates this
├── const.py             # all constants — DOMAIN, API hosts, config-entry keys, data keys
├── config_flow.py       # PKCE authorize URL → user pastes callback → exchange → select_contract
├── coordinator.py       # HaggleCoordinator: 30-day backfill + incremental statistics import
├── sensor.py            # 6 SensorEntityDescription entries; HaggleEnergySensor
├── agl/
│   ├── __init__.py
│   ├── client.py        # AglAuth (JWT expiry + token rotation) + AglClient (HTTP methods)
│   ├── models.py        # TokenSet, Contract, IntervalReading, DailyReading, BillPeriod, PlanRates
│   ├── parser.py        # JSON → typed dataclasses (filters type=none intervals)
│   └── pinning.py       # SPKI extraction helper for Trust-On-First-Use TLS pinning
├── strings.json         # translatable config-flow strings
└── translations/en.json # English strings (must mirror strings.json)

tests/
├── conftest.py                      # _auto_enable_custom_integrations fixture
├── fixtures/
│   ├── hourly_response.json         # 30-min interval data (Current/Hourly)
│   ├── overview_response.json       # /v3/overview with accounts + contracts
│   ├── plan_response.json           # /v2/plan/energy with gstInclusiveRates
│   └── bill_period_response.json    # usage summary
├── test_init.py                     # setup/unload smoke tests
├── test_config_flow.py              # PKCE step navigation (user → exchange → select_contract)
├── test_agl_client.py               # AglAuth token rotation + AglClient HTTP methods + pin-check wiring
├── test_const.py                    # base64 sanity-check on AGL_AUTH0_CLIENT
├── test_parser.py                   # parse_interval_readings, parse_overview, parse_plan, _safe_float
├── test_pinning.py                  # SPKI extraction + host-name guards
└── test_coordinator_statistics.py   # backfill, incremental resume, idempotency, numeric guards

scripts/
├── wt                   # bash worktree helper (new / list / rm)
└── validate_manifest.py # used by the validate-manifest Claude hook

.claude/
├── settings.json        # committed hooks config
├── agents/              # 8 subagent definitions (5 domain + 3 review)
└── commands/            # 5 slash commands (new-entity, wt, release, hassfest, pr)

.github/
├── workflows/
│   ├── ci.yml           # ruff + mypy + pytest matrix (Python 3.13)
│   ├── hacs.yml         # HACS validation
│   ├── hassfest.yml     # Home Assistant integration manifest validation
│   ├── release.yml      # tag-triggered GitHub Release + build-provenance attestation
│   └── codeql.yml       # weekly + per-PR CodeQL Python scan
├── CODEOWNERS           # @naanyabiz owns everything
└── dependabot.yml       # weekly pip + github-actions updates

# Repo-root posture files
SECURITY.md              # disclosure path + threat-model summary
CONTRIBUTING.md          # dev loop + commit conventions + PR checklist
CODE_OF_CONDUCT.md       # Contributor Covenant 2.1
```

---

## Documentation Checklist — Required on Every PR

Every PR that ships code (not pure CI/tooling fixes) MUST include updates to
all of the following before it can be merged. The `/pr` command enforces this.

| Artifact | What to update | Where |
|---|---|---|
| `CHANGELOG.md` | Add bullet(s) under `## [Unreleased]` for every user-visible capability added, changed, or fixed | repo root |
| `AGENTS.md` — Repo Map | Add any new files; update descriptions if a file's role changed | this file |
| `AGENTS.md` — AGL API | Correct any API facts that were proven wrong (endpoints, field names, token lifetimes, headers) | this file |
| `AGENTS.md` — What NOT to Do | Add a new prohibition if a footgun was discovered | this file |
| Memory files | Record non-obvious decisions, confirmed API behaviour, or user preferences that should survive context resets | `~/.claude/projects/.../memory/` |

**Sprint / phase boundary** (when a branch completes a named sprint or phase):

- Move completed items out of `## [Unreleased]` into a dated `## [x.y.z-dev]` entry.
- Update `## [Unreleased]` → `### Targets for next sprint` with the next block of work.
- Verify the Repo Map matches every file currently in `custom_components/haggle/` and `tests/`.
- Review every bullet in the AGL API section against the current implementation — correct or delete stale facts.

---

## Subagent Triggers

| Agent | File | Trigger condition |
|---|---|---|
| `ha-integration-architect` | `.claude/agents/ha-integration-architect.md` | Edits to `__init__.py`, `config_flow.py`, `coordinator.py`, `sensor.py`; HA-pattern questions |
| `agl-api-explorer` | `.claude/agents/agl-api-explorer.md` | Any work in `agl/`; new AGL endpoints; raw HTTP questions |
| `energy-domain-expert` | `.claude/agents/energy-domain-expert.md` | `state_class`, `device_class`, `unit_of_measurement` changes; `import_statistics()` usage |
| `ha-test-writer` | `.claude/agents/ha-test-writer.md` | After every change in `custom_components/haggle/`; proactively |
| `release-manager` | `.claude/agents/release-manager.md` | Only via `/release` command |
| `code-quality-reviewer` | `.claude/agents/code-quality-reviewer.md` | Non-trivial edits in `custom_components/haggle/`; before opening a PR |
| `security-reviewer` | `.claude/agents/security-reviewer.md` | Edits in `config_flow.py`, `agl/`, `__init__.py`; any change touching tokens, auth, HTTP, or logging |
| `async-performance-reviewer` | `.claude/agents/async-performance-reviewer.md` | Edits in `coordinator.py`, `agl/client.py`, or any async function |

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

## GitHub Issues Workflow

GitHub issues are the canonical place to track non-trivial work that
isn't being done right now. This is deliberate: a CHANGELOG entry, a
memory note, or an inline `# TODO` comment all rot quickly and are
invisible to anyone who doesn't already know to look.

**Open an issue when:**
- A docs gap, chore, or process improvement is discovered mid-sprint and
  is not in scope of the current PR.
- A footgun is found that future agents need to be warned about (also
  add it to "What NOT to Do" if it's actionable).
- A code-review note is "do this next round" rather than "do this now".
- A bug reproduces but you don't have time to fix it this PR.

**Don't:**
- Use `# TODO` comments in committed code for tracking work — they have
  no due date and no owner.
- Use CHANGELOG `## [Unreleased]` as a TODO list — it ships in the next
  release notes; bullets there should describe done work.
- Use memory files for tracking — memory captures durable design
  decisions and confirmed API behaviour, not work items.

**PRs close issues explicitly.** Use `Closes #N` in the PR body so
GitHub auto-closes on merge. If a PR partially addresses an issue,
comment on the issue rather than closing it.

When mid-sprint code-review or audit work surfaces a tail of items,
spawn issues for each one and label-and-prioritise them rather than
trying to fold everything into the current PR.

---

## AGL API — Key Facts

### Authentication (Auth0 PKCE)

- **Auth host**: `https://secure.agl.com.au`
- **Setup grant**: `authorization_code` + PKCE (`S256`). The config flow
  generates a PKCE verifier+challenge, builds an `/authorize` URL, and shows
  it to the user. The user opens the URL in their **real browser** (handles
  Akamai bot-protection + MFA transparently), then pastes the callback URL back.
  The integration extracts the `code` and POSTs to `/oauth/token`.
  - `redirect_uri`: `https://secure.agl.com.au/ios/au.com.agl.mobile/callback`
  - `scope`: `openid profile email offline_access`
  - `audience`: `https://api.platform.agl.com.au/` (trailing slash required)
- **Ongoing grant**: `refresh_token` (stored in `entry.data`).
- **Token endpoint**: `POST /oauth/token`
- **client_id**: `2mDkNcC8gkDLL7FTT1ZxF5rrQHrLTHL3` (documented 2026-04-30)
- **Required headers**: `Client-Flavor: app.iOS.public.8.38.0-531`
- **Access token**: JWT (RS256), `exp` = **15 min** (`expires_in: 900` — confirmed 2026-05-01). Decode `exp`; refresh 2 min early.
- **CRITICAL — token rotation**: Auth0 **rotates** the refresh token on every
  exchange. The integration MUST persist the new refresh token via
  `_persist_refresh_token` callback after every exchange or it will lock
  itself out on the next restart.

### Data API

- **Base**: `https://api.platform.agl.com.au`
- **Required headers on ALL data endpoints** (documented from AGL mobile app 8.38.0-531, 2026-05-01):
  - `Client-Flavor: app.iOS.public.8.38.0-531`
  - `Client-Device: Apple-iPhone-iPhone14,7-iOS-26.4.2`
  - `Accept-Language: en-AU,en;q=0.9`
  - `Accept-Features: <long feature-flag list>` — see `AGL_ACCEPT_FEATURES` in `const.py`.
    Must include `UsageEnableHistoricalMeterReads`. **Omitting any of these headers causes
    HTTP 500 on Hourly/Daily usage endpoints** (overview and plan are more permissive).
- **`scaling` query parameter**: Hourly and Daily usage URLs require
  `&scaling=36.514404_108.057_40.670903_120.357_0_0_0_0` (screen DPI vector for chart
  rendering). Without it, the BFF returns HTTP 500.
- **Contract discovery**: `GET /mobile/bff/api/v3/overview`
  - Key fields: `accounts[].accountNumber`, `accounts[].contracts[].contractNumber`
  - `contractNumber` ≠ `accountNumber` — use `contractNumber` in all data paths
- **30-min interval data** (despite "Hourly" in the path):
  `GET /mobile/bff/api/v2/usage/smart/Electricity/{contractNumber}/Current/Hourly?period=YYYY-MM-DD_YYYY-MM-DD&scaling=...`
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
| Token refresh | Just-in-time (< 2 min to `exp`) | tokens expire at 15 min |

**Do not poll for today's hourly data** — it will be empty. Fetch *yesterday*.

### Previous Bill Period

```
GET /mobile/bff/api/v2/usage/smart/Electricity/{contractNumber}/Previous/Hourly?period=YYYY-MM-DD_YYYY-MM-DD&scaling=...
```

Used for backfill of dates **before** the current billing period start (`bill_period.start`).
Confirmed working back to at least 2025-12-24 (single-day period params). Requires the same
`Accept-Features`/`Client-Device`/`scaling` headers as `Current/Hourly`.

### Plan / Rates

```
GET /mobile/bff/api/v2/plan/energy/{contractNumber}
```

Returns `gstInclusiveRates` list with `c/kWh` and `c/day` entries. Supply charge
is a `c/day` entry with `title` containing "Supply charge".

### TLS pinning (Trust-On-First-Use)

Both `secure.agl.com.au` and `api.platform.agl.com.au` are pinned by SPKI hash.
Capture happens inside `agl/pinning.py::HagglePinningConnector` — a
`TCPConnector` subclass that overrides `_wrap_create_connection`. After every
new TLS handshake the connector extracts the leaf-cert SPKI from
`transport.get_extra_info("ssl_object")` and stores it in `connector.observed[host]`.
An optional `on_new_connection(host, spki)` callback fires synchronously so
callers can validate against a stored TOFU pin.

The persisted hashes live in `entry.data` under `CONF_PINNED_SPKI_AUTH` and
`CONF_PINNED_SPKI_BFF`. They are read in `config_flow._exchange_code` /
`_fetch_contracts` (each uses a one-shot `aiohttp.ClientSession(connector=…)`
and reads `connector.observed[host]` after the call) and validated at runtime
by the long-lived session in `__init__.py::async_setup_entry`.

**Mismatch is warn-only** — log a WARNING + emit an HA persistent notification
(`haggle_pin_mismatch_<host>`) — but the request still succeeds. This keeps a
legitimate AGL cert rotation from bricking HACS users; the documented
remediation is to re-run Reconfigure on the integration card, which re-captures
both hashes.

Empty stored values (`""`) mean "no pin yet" — the validator is a no-op.
Older entries created before this feature land in this state and silently
upgrade on next Reconfigure.

**Why a connector subclass and not `resp.connection`?** aiohttp releases the
`Connection` back to its pool the moment a response is constructed, so
`resp.connection` (and `resp._protocol.transport`) are already `None` by the
time `async with session.get(...) as resp:` enters. The first cut of TOFU
pinning shipped with that bug — every live install was running with empty
SPKI strings (verified 2026-05-03) — until the connector subclass redesign.
Tests must use a real local TLS server (see `tests/test_pinning.py`); mocking
`resp.connection` will not catch this lifecycle issue.

---

## Energy Dashboard Contract

The HA Energy dashboard requires:
- `device_class = ENERGY`, `state_class = TOTAL_INCREASING`, `native_unit_of_measurement = kWh`
- Historical data MUST be fed via `async_add_external_statistics()` (not live state updates).
  AGL data is always historical — the recorder writes it to the correct UTC hour slot
  regardless of when the API call happened. Skipping this means the Energy dashboard shows
  a spike at poll time, not a smooth historical chart.
- Statistic IDs per contract:
  - `haggle:consumption_<contract_number>` — kWh, `has_sum=True`, **`unit_class="energy"`**
  - `haggle:cost_<contract_number>` — AUD, `has_sum=True`, `unit_class=None`
- **`unit_class="energy"` is required** on the consumption statistic for it to appear in
  the Energy dashboard's "add consumption source" picker. `unit_class=None` silently excludes
  it from the UI filter even though the data is in the DB.
- Resume point: `get_last_statistics(hass, 1, stat_id, True, {"start", "sum"})` — returns
  the last-imported hour so incremental updates don't re-import already-stored rows.
- Each import call is idempotent: `(statistic_id, start)` updates in place.

---

## What NOT to Do

- **No `requests`** — always `aiohttp`. Blocking I/O in the event loop will freeze HA.
- **No blocking I/O in the coordinator** — `_async_update_data` must be fully async.
- **No OTP/portal flow** — auth is PKCE via the user's real browser, not portal scraping.
- **No hardcoded contract numbers** — they come from `/v3/overview` at config time.
- **No polling faster than 24 h for interval data** — AGL won't have newer data.
- **Don't store `access_token` in `entry.data`** — it's transient (15 min).
  Persist only `refresh_token` to `entry.data`; keep `access_token` in memory only.
- **Don't use `async_add_executor_job`** for AGL API calls — they're already async.
- **Don't pass `access_token` to `AglAuth`** — `AglAuth.__init__` expects a `refresh_token`.
  Passing an `access_token` silently fails: `async_force_refresh` posts it as a refresh_token,
  Auth0 rejects it, and the contract number is never set → HTTP 404 on every data call.
  For one-shot calls with a bare bearer token (e.g. config flow), use a direct `aiohttp` GET.
- **Don't omit `Accept-Features` / `Client-Device` / `scaling`** — omitting any of these
  from Hourly or Daily usage requests returns HTTP 500 with no useful error body.
- **Don't set `unit_class=None` on the consumption statistic** — HA's Energy dashboard
  consumption picker filters by `unit_class="energy"`. `None` silently hides the statistic.
- **No committing directly to `main`** — the `guard-main-branch` hook blocks it.
  Use a feature branch + PR.
- **No mutable GitHub Action refs** — pin every `uses: owner/action@…` to a
  40-char commit SHA with a `# vX.Y` comment. `@main`, `@master`, and floating
  major tags (`@v6`) are all branch-poisonable supply-chain vectors. Dependabot
  (`github-actions` ecosystem) keeps the SHAs current.
- **Don't surface raw AGL/Auth0 response bodies in exceptions** that propagate
  to `ConfigEntryAuthFailed` / `UpdateFailed`. They reach HA Persistent
  Notifications and `home-assistant.log` at ERROR level. Auth0 5xx/429 bodies
  can include diagnostic fields (`mfa_token`, internal trace IDs); AGL BFF URLs
  carry the contract number (PII). Pattern:
  `_LOGGER.debug("…body: %s", text[:200]); raise AGLError(f"HTTP {status} …")`.
- **Don't use unbounded `float()` coercion on AGL response values**. Use the
  `_safe_float` helpers in `agl/parser.py` / `coordinator.py` so `inf`/`nan`/
  negative values can't reach `async_add_external_statistics` and corrupt the
  cumulative-sum series.
- **Don't forward raw AGL response dicts** via `dict(rate)` or similar
  open-schema passthrough. Allowlist exactly the fields the coordinator
  consumes, so a MITM-crafted response can't smuggle keys into runtime state.

---

## Contributing — Adding a New Endpoint

To add support for a new AGL API endpoint:

1. **Identify the endpoint contract** from your own AGL account. Any standard
   HTTP-debugging tool of your choice is fine — you only need the resulting URL
   path, required headers, and JSON response shape.
2. **Anonymise before committing**: redact `accountNumber`, `contractNumber`,
   address, product code, and any meter-read timeseries that fingerprint a real
   residence. Use the placeholders in `tests/fixtures/overview_response.json`
   as the canonical set (`1234567890` / `9999999999` / `1 Sample Street SUBURB QLD 4000`).
3. Add an anonymised fixture under `tests/fixtures/<name>_response.json`.
4. Add a parser in `agl/parser.py` and a corresponding `AglClient` method in
   `agl/client.py`.
5. Add tests against the fixture. Do not commit any captures with real customer
   values.

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
maintainer (@naanyabiz). All commits carry `Co-Authored-By: Claude` trailers.
The integration is built against the API responses returned to a legitimate AGL
customer using AGL's own mobile client endpoints. No proprietary AGL code is
included. Anonymised response shapes are mirrored under `tests/fixtures/`; the
full API contract is documented in the "AGL API — Key Facts" section above.
