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

# Wire the git hooks (once) — pre-commit + commit-msg stages
# (provenance trailer, Conventional Commits); pre-push stays opt-in
uv run pre-commit install

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

Test strategy (what layer of testing a change needs, coverage floor,
when live-HA manual testing is required): [docs/testing.md](docs/testing.md).

Control expectations: this repository operates under the committed
[secure-SDLC standard](docs/compliance/secure-sdlc-standard.md); every
`CO-x.y` reference in this tree resolves there. The statement-level
[conformance map](docs/compliance/conformance.md) records how each control
is met (or the recorded exception) — a PR that changes a control surface
(workflow, ruleset baseline, agent grant, release gate, security doc)
updates the affected conformance rows in the same PR.

---

## Repo Map

```
custom_components/haggle/
├── __init__.py          # async_setup_entry / async_unload_entry / async_remove_entry + HaggleRuntimeData; _check_pin reads pins live from entry.data; async_migrate_entry (minor 1→2: CONF_LOCAL_TZ derived from the entry title, #268); setup resolves the contract tz CONF_LOCAL_TZ → HA tz (fallback flagged tz_is_contract=False)
├── manifest.json        # HACS/HA metadata; hassfest validates this
├── const.py             # all constants — DOMAIN, API hosts, config-entry keys (incl. CONF_LOCAL_TZ, the contract-tz authority), AGL_API_TZ_KEY (the zone AGL converts dateTime through, #292), data keys
├── config_flow.py       # PKCE authorize URL → user pastes callback → exchange → select_contract (electricity-only via _serviceable_contracts, #260); reauth via its own `reauth_confirm` step (#284; token only, fills missing pins) + reconfigure (token + re-pin, dismisses pin-mismatch notices): entry contract matched by number, no picker, async_update_reload_and_abort(data_updates=…) (#275); options flow (solar statistics-writes toggle, poll-interval throttle); MINOR_VERSION 2 — stores CONF_LOCAL_TZ from the selected contract's address at creation (fast path + picker) and refreshes it on reauth/reconfigure (#292/#268)
├── diagnostics.py       # anonymized config-entry diagnostics (schema v3 — adds coordinator.contract_timezone + entry.data.local_tz) — public-safe; parsed by the triage routine (docs/diagnostics.md)
├── coordinator.py       # HaggleCoordinator: 30-day backfill (throttled, 429-aware, per-series ranges) + incremental statistics import (aggregate + per-tariff ToU series + solar generation/credit on hasSolar contracts) + bill-period solar totals + half-hour-zone straddle guard (overlap day + content-derived trim, #292) + contract-local midnight helper _local_midnight_utc (#268) + stale-key fill on every import (_stale_key_fill: provenance-gated zero / carry-forward re-chain of stored rows the batch has no value for, #300)
├── sensor.py            # 14 SensorEntityDescription entries (3 conditional ToU rate sensors, 5 conditional solar sensors); HaggleEnergySensor
├── agl/
│   ├── __init__.py
│   ├── client.py        # AglAuth (JWT expiry + token rotation) + AglClient (HTTP methods; local_tz + tz_is_contract, set_contract_tz)
│   ├── models.py        # TokenSet, Contract, IntervalReading, DailyReading, BillPeriod, PlanRates
│   ├── parser.py        # JSON → typed dataclasses; TOTAL over arbitrary JSON (fuzz-enforced) — filters type=none intervals; label-keyed bill projection (_projection_label, #253); Sydney-conversion inverse relocalise_agl_timestamp keyed on the response's timeZone (#292)
│   └── pinning.py       # SPKI extraction helper for Trust-On-First-Use TLS pinning
├── strings.json         # translatable config-flow strings
└── translations/en.json # English strings (must mirror strings.json)

tests/
├── conftest.py                      # _auto_enable_custom_integrations fixture
├── fixtures/
│   ├── PROVENANCE.md                 # fixture provenance + the solar capture consent note
│   ├── hourly_response.json         # 30-min interval data (Current/Hourly)
│   ├── overview_response.json       # /v3/overview with accounts + contracts
│   ├── plan_response.json           # /v2/plan/energy with gstInclusiveRates (flat rate)
│   ├── tou_plan_response.json       # Time-of-Use plan — per-band gstInclusiveRates
│   ├── tou_hourly_response.json     # mixed peak/offpeak/shoulder/normal intervals
│   ├── solar_hourly_response.json   # REAL full-day ElectricitySolar capture (2026-07-01, app-reconciled) — provenance + consent: fixtures/PROVENANCE.md
│   ├── solar_plan_response.json     # solar plan — feed-in rate in gstExclusiveRates
│   ├── overview_solar_response.json # /v3/overview variant with hasSolar: true
│   └── bill_period_response.json    # usage summary
├── test_init.py                     # setup/unload smoke tests + minor-version migration (CONF_LOCAL_TZ from title) + stored-tz resolution at setup
├── test_config_flow.py              # PKCE step navigation (user → exchange → select_contract) + reauth/reconfigure update-in-place + CONF_LOCAL_TZ stored on creation/reauth/reconfigure
├── test_agl_client.py               # AglAuth token rotation + AglClient HTTP methods + pin-check wiring
├── test_const.py                    # base64 sanity-check on AGL_AUTH0_CLIENT
├── test_parser.py                   # parse_interval_readings, parse_overview, parse_plan, ToU rate mapping, safe_float, relocalise_agl_timestamp zone/DST vectors + window interaction (#292)
├── test_pinning.py                  # SPKI extraction + host-name guards
├── fuzz/
│   ├── fuzz_parser.py               # atheris harness — parser totality + numeric guards (run by fuzz.yml)
│   └── requirements.txt             # hash-pinned atheris (Scorecard Pinned-Dependencies)
├── test_coordinator_statistics.py   # backfill, incremental resume, idempotency, ToU per-tariff series, numeric guards, half-hour-zone overlap/trim + contract-local midnight sites (#292)
├── test_recorder_statistics.py      # sum-chain scenarios vs the REAL recorder (recorder_mock) — spike/#114/ToU-partition/half-hour-straddle classes + #300 stale-key fill (SA/QLD-DST convention re-key, provenance gate, straddle both-days, conservation guard, read budget)
├── test_sensor.py                   # sensor descriptions + conditional ToU rate-sensor registration
├── test_claude_hooks.py             # hook sanitization (#244), guard robustness, TOFU verify-wiring fail-closed (#245) — executes the literal shipped scripts/commands
├── test_provenance_hook.py          # executes scripts/check_provenance_trailer.sh against sample messages (any-vendor Co-Authored-By / AI-Assisted: none / merge skip)
└── test_diagnostics.py              # leak tests (token/contract/account/SPKI never serialize) + schema v3 shape

docs/
├── compliance/
│   ├── secure-sdlc-standard.md  # the standard this repo operates under — 19 COs / 94 statements, industry-agnostic (v1.0)
│   └── conformance.md           # statement-level conformance map: mechanism per statement + RA-xx exceptions — update rows in the SAME PR as any control-surface change
├── energy-dashboard.md  # user guide — which haggle:* statistics to add per plan type, sensor glossary, troubleshooting (#137 footgun)
├── delivery-metrics.md  # quarterly delivery-metrics process + recorded time-to-restore exception (CO-18.3)
├── releasing.md         # release acceptance policy — beta-soak rule, hotfix evidence rule, downgrade test, acceptance record
├── testing.md           # test strategy — four layers, coverage floor, when live-HA manual testing is required
├── diagnostics.md       # diagnostics schema v3 reference — users + triage routine (bump with DIAGNOSTICS_SCHEMA_VERSION)
├── threat-model.md      # living threat model — trust boundaries, STRIDE register + dispositions, AI agents, regulatory scope, resilience targets
└── agents/
    ├── triage-routine.md    # authoritative spec of the haggle-triage routine (repo-first change control, CO-12.8) — edit HERE, then sync the platform copy
    └── injection-corpus.md  # canned hostile payloads + manual replay procedure — run before ANY triage-prompt change

scripts/
├── delivery_metrics.py  # quarterly CO-18.3 delivery metrics + CHANGELOG/tag/release reconciliation (docs/delivery-metrics.md)
├── wt                   # bash worktree helper (new / list / rm)
├── pin-hooks.sh         # re-pin .claude hook scripts + install wiring into settings.local.json AFTER reviewing diffs (#245 TOFU control)
├── access-review.sh     # quarterly access review (SECURITY.md "Access Review") — asserts the expected access surface + prints the manual checklist; read-only, maintainer-run with local gh auth, deliberately not CI
├── check_provenance_trailer.sh  # commit-msg hook: every commit names its AI tool (Co-Authored-By) or declares AI-Assisted: none (#283); tested by tests/test_provenance_hook.py
├── export-settings.sh   # admin-run: re-export control-plane baselines into .github/settings/ (PR-first on any settings change)
├── normalize-ruleset.jq / normalize-repo-public.jq  # shared normalizers (export script + settings-drift workflow)
└── validate_manifest.py # used by the validate-manifest Claude hook

.claude/
├── settings.json        # committed permission policy (permissions ONLY — hook wiring deliberately lives in the untracked settings.local.json, see #245)
├── hooks-wiring.json    # policy record for the hook wiring; installed into settings.local.json by scripts/pin-hooks.sh after review
├── hooks/               # the four hook scripts — TOFU-pinned via .claude/hooks.sha256 (untracked)
├── agents/              # 8 subagent definitions (5 domain + 3 review)
└── commands/            # 5 slash commands (new-entity, wt, release, hassfest, pr)

.github/
├── settings/            # declared state of the GitHub control plane (rulesets, repo settings) — see settings/README.md; weekly drift check
├── workflows/
│   ├── ci.yml           # ruff + mypy + pytest (Python 3.14, coverage floor 89) + gitleaks full-history scan + dependency-review + shellcheck/actionlint/zizmor
│   ├── hacs.yml         # HACS validation
│   ├── hassfest.yml     # Home Assistant integration manifest validation
│   ├── release.yml      # tag-triggered Release (first-party gh CLI): tag-on-main + tag-signature gates, HACS-installed attested zip (zip_release), SBOM attestations, check-run snapshot
│   ├── codeql.yml       # weekly + per-PR CodeQL Python scan
│   ├── compat.yml       # weekly non-blocking suite vs latest phcc/HA (incl. beta) — early upstream-breakage warning
│   ├── scorecard.yml    # weekly + on-push OpenSSF Scorecard self-assessment (feeds README badge)
│   ├── fuzz.yml         # weekly deep run + unconditional 120s PR smoke; corpus cached across runs; crash artifacts uploaded
│   └── settings-drift.yml # weekly: re-export rulesets + public repo settings, diff vs .github/settings/, issue on drift
├── CODEOWNERS           # @naanyabiz owns everything
└── dependabot.yml       # weekly pip + github-actions updates, grouped into one PR per ecosystem

# Repo-root posture files
.gitleaks.toml           # repo-specific secret rules (Auth0 refresh tokens, real AGL account/contract numbers) layered on gitleaks defaults
SECURITY.md              # disclosure path + threat-model summary
CONTRIBUTING.md          # dev loop + commit conventions + PR checklist
CODE_OF_CONDUCT.md       # Contributor Covenant 2.1
ROADMAP.md               # ~12-month direction + explicit non-goals (single-retailer AGL, read-only, no telemetry)
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
| `SECURITY.md` + `docs/threat-model.md` | Update when a change alters the security posture, trust boundaries, or accepted risks (new endpoint, scope, storage location, data field, agent, or gate exception) | repo root + `docs/` |

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

Main worktree (`~/projects/haggle/`) is always on `main`. Feature
work happens in sibling worktrees at `~/projects/haggle.wt/<branch>/`.
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

Non-trivial feature issues state acceptance criteria up front (the
feature template has an optional field; if it is left empty the
maintainer states them on the issue before implementation); the closing
PR's test plan references them.

When mid-sprint code-review or audit work surfaces a tail of items,
spawn issues for each one and label-and-prioritise them rather than
trying to fold everything into the current PR.

**Label taxonomy (applied at triage, kept current).** Every open issue
carries exactly one priority label from the moment it is triaged:

- `P1` — next release / blocks the next milestone
- `P2` — within the next few releases
- `P3` — opportunistic; pick up when the area is next touched

Priority records *consequence to users*, not effort. Defects additionally
carry exactly one severity label — `sev:high` (wrong energy/cost data
written to statistics, auth lockout, or integration down), `sev:med`
(feature degraded or misleading; workaround exists), `sev:low` (cosmetic)
— plus `escaped` if the defect existed in a published release (the bug
form's required "Haggle version" field answers this; anything a user hit
in the wild is escaped). Known shortcuts and unvalidated assumptions get
`debt`. Two consumers depend on these labels being accurate: the
`/release` flow counts closed `escaped` issues into each release's
CHANGELOG section, and `scripts/delivery_metrics.py` computes the
quarterly delivery metrics (see `docs/delivery-metrics.md`).

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
- **Revocation on removal**: `async_remove_entry` makes a best-effort
  `POST /oauth/revoke` (public client — `client_id` + `token` JSON body, no
  secret) so the grant does not outlive uninstall; with rotation enabled
  Auth0 revokes the whole token family. All failures swallowed by design.
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
- **kWh source of truth**: `consumption.quantity` (outer) — matches the AGL
  portal "MyUsageData" CSV export to 0.001 kWh. Reconciled 2026-05-12 across
  11 mitm /Hourly captures.
  - **Do NOT use `consumption.values.quantity`** (inner). It's a DPI/chart-scaled
    helper (in real captures `values.amount` always equals `values.quantity`)
    that undercounts kWh by 4-73% with no consistent ratio. Reading it was the
    root cause of the v0.1.0 / v0.2.0-beta.{1,2,3} meter-undercounting bug.
- **Cost source of truth**: `consumption.amount` (outer) — AUD for the slot.
- **`timeZone` field** (top-level): `"Australia/Sydney"` in EVERY usage
  response seen — Hourly and Daily, Current and Previous, a Queensland
  contract's included. It is the zone AGL converts `dateTime` through (next
  bullet), NOT the contract's zone.
- **`dateTime` field — NOT the slot start in true UTC** (#292). It is the
  meter's LOCAL wall-clock slot label converted to UTC through the
  response's `timeZone` (Australia/Sydney), whatever the contract's zone.
  CONFIRMED two ways: the #292 Adelaide reporter reconciled 887 hourly
  totals against the portal CSV only when the CSV's local labels were read
  as Sydney time (DST-aware; TeslaMate corroborates the CSV labels are
  Adelaide local), and the maintainer's raw Queensland captures (held
  locally, not committed) show Dec–Mar local days running
  `D-1T13:00Z → DT12:30Z` — Sydney AEDT midnight, an hour before Brisbane's
  — and April days `D-1T14:00Z → DT13:30Z` (AEST, identical for both). The
  `period=` query IS still interpreted in the contract's local day. How
  early the raw value is, by contract zone:

  | Contract zone | Raw `dateTime` is early by | Status |
  |---|---|---|
  | NSW / VIC / TAS / ACT | 0 — identity at every instant | by construction |
  | QLD | 0 Apr–Oct; **1 h** while Sydney is on DST (first Sun Oct → first Sun Apr) | **confirmed** (raw captures) |
  | SA / Broken Hill | **30 min** all year | **confirmed** (#292 reporter) |
  | NT | 30 min Apr–Oct; 90 min in Sydney DST | derived |
  | WA | 2 h Apr–Oct; 3 h in Sydney DST | derived |

  `parser.relocalise_agl_timestamp` inverts it — read the instant as a
  wall-clock label in the API zone, re-attach the contract's zone, express
  in UTC again, `fold` preserved — immediately after the parse and BEFORE
  the window check, using the response's own `timeZone` (bounded,
  untrusted; `AGL_API_TZ_KEY` as the fallback) so it self-corrects if AGL
  ever converts per contract. Applied ONLY when the zone is address-derived
  (`AglClient.tz_is_contract`); under the HA-timezone fallback timestamps
  are left as-is — loud (dropped slots + WARNING), as before — rather than
  shifted silently. For every zone `tz_for_address` can produce, the
  correction never moves a reading EARLIER (verified over every 30-min
  instant of 2026), so the no-leading-slack invariant holds unchanged. ONE
  bounded DEBUG line per parse records the first raw → corrected pair
  (`api_tz`, declared `timeZone`, `contract_tz`) as the evidence hook. Two
  once-a-year residuals are AGL's, not ours: on a fall-back day (first Sun
  Apr) AGL may collapse both 02:xx folds onto one and lose a slot; on
  Sydney's spring-forward day a no-DST zone's real 02:00–02:59 has no
  Sydney label and cannot be recovered by any inverse. Exposure: SA
  installs have written shifted data since v0.1.0; the first public
  release (v0.1.0, 2026-05-02) post-dates the 2026 DST end (2026-04-05),
  so QLD installs first wrote shifted data on 2026-10-04 — a 30-day
  in-place re-alignment (the
  follow-up PR) covers the whole QLD span only if users upgrade by about
  3 November 2026.
- **Daily endpoint `dateTime`** (`/Current/Daily`, `/Previous/Daily`): the
  LOCAL calendar date with a literal `00:00:00Z` — a date label, not an
  instant. Verified on raw captures from both DST seasons: every item is
  `YYYY-MM-DDT00:00:00Z`, and each day's `consumption.quantity` equals the
  Hourly day-query sum for the same label (2026-03-06 → 85.869 kWh,
  2026-04-28 → 29.044 kWh). `.date()` is therefore already the local day
  and the Sydney re-localisation must NOT be applied. `parse_daily_readings`
  is unused at runtime — `AglClient` has no Daily method.
- **`consumption.type`**: `normal` | `peak` | `offpeak` | `shoulder` | `none`
  (filter out `none` — future-dated or unavailable intervals)
- **Zero-on-zero filter**: AGL also returns intervals with non-`none` type
  but both `quantity` and `amount` equal to 0 for days where the AEMO feed
  hasn't yet delivered the meter reads. The parser drops these (they would
  otherwise create phantom flat rows that the resume logic would skip past
  permanently once AGL backfilled the real reads).

### Polling Cadence

| Data | Interval | Reason |
|---|---|---|
| 30-min intervals | 24 h floor, user-configurable up to 168 h (7 days) | AGL data is delayed 24-48 h (AEMO feed lag), so nothing below 24 h finds anything new — see `OPT_POLL_INTERVAL_HOURS` (#228) below |
| Plan / overview | 7 days | Rarely changes |
| Token refresh | Just-in-time (< 2 min to `exp`) | tokens expire at 15 min |
| **After a FAILED poll** | 30 min (`RETRY_INTERVAL_ON_ERROR`) | #155: a transient error previously cost a full 24 h and looked like "the poll never ran" (#126). Restored to the configured cadence on the next success; auth failures go to reauth, not fast retry |

**User-configurable poll interval (#228)**: the integration's Options
(`config_flow.py::HaggleOptionsFlow`) expose `poll_interval_hours`, read LIVE
by the coordinator each cycle via `HaggleCoordinator._configured_poll_interval`
— same no-reload-listener pattern as `OPT_SOLAR_STATISTICS_ENABLED`, takes
effect from the next poll. Only ever lengthens the cadence from the 24 h
default (for users who want to throttle back AGL request volume); the 24 h
floor is enforced twice — the options-flow `vol.Range` and a defensive clamp
in `coordinator._clamped_poll_interval` (`const.py`: `MIN_POLL_INTERVAL_HOURS`
/ `MAX_POLL_INTERVAL_HOURS`) — so a hand-edited `entry.options` value can't
violate it either.

**Do not poll for today's hourly data** — it will be empty. Fetch *yesterday*.

**Trailing rewindow (self-healing)**: once initial backfill is complete, every
poll re-fetches the trailing `REWINDOW_DAYS` (default 7). This makes the
integration self-heal AGL's day-late AEMO backfills — a slot first returned as
a `quantity=0` placeholder is overwritten with the real meter read on a later
cycle. `async_add_external_statistics` is idempotent on `(statistic_id, start)`
so the overwrite is safe.

The cumulative-sum baseline for the import (aggregate AND every per-tariff
series) is looked up in `_import_intervals` via `statistics_during_period`
using the **actual earliest fetched-interval hour** as the cutoff — NOT a
`fetch_start`-derived UTC midnight, and NOT the most-recent stored sum. AGL's
`period=` query is interpreted in the contract's local timezone, so the first
interval of a day query lands at local midnight in UTC (e.g.
`(fetch_start - 1)T14:00Z` for an AEST account, `T14:30Z` for Adelaide once
the #292 re-localisation has run). A cutoff fixed at
`fetch_start T00:00Z` UTC folded ~10 h of about-to-be-overwritten old sums into
the baseline and the new chain re-added those hours' deltas, producing a phantom
`+N kWh` jump in the recorder `sum` column every local midnight (the Energy
dashboard renders hourly deltas as `sum[h] - sum[h-1]`, so the spike was
visible there). Using the earliest fetched hour is correct regardless of
timezone or DST.

The baseline lookup itself (`_baseline_sums_before`) is **two-stage**: a cheap
batched window of `look_back_days` ending at the cutoff (2 days for the
aggregate, `BACKFILL_DAYS` for per-tariff series), and — only for a series with
NO rows in that window — a reach-back lookup from the start of recorded history.
Both stages stay strictly *before* the cutoff, so neither ever reads a sum from
inside the rewindow rows about to be rewritten (this is why `get_last_statistics`
is wrong here). Without the reach-back, a ToU band absent for longer than the
window and then reappearing inside the rewindow would reset its cumulative sum to
0.0 — a downward step breaking that series' `TOTAL_INCREASING` monotonicity
(#114, fixed v0.3.2).

**Half-hour zones (SA / Broken Hill / NT)** (#292): after re-localisation
the contract-local midnight lands at `:30` past the UTC hour (`14:30Z` /
`13:30Z` for Adelaide), so the hourly statistics bucket at every local-day
boundary is shared by two days — `_bucket_hourly` floors to the UTC hour and
HA rejects any row not on the hour. A batch that starts at day D holds only
its own half of that bucket, and because imports overwrite
`(statistic_id, start)` in place and the baseline is the row BEFORE
`min(hour_cons)`, the previous day's 23:30 slot left the sum chain for good
on every sliding rewindow, chunk boundary and big-gap resume (reproduced on
the real recorder: 96 → 95 kWh per sliding day). The loss was invisible
while AGL's Sydney conversion aligned SA to whole hours — correcting the
timestamps exposes it, so the guard ships with the correction:

- **Overlap day**: `_fetch_range` starts each series range one day earlier
  (`S-1`) when `_local_midnight_utc(S).minute != 0` and `S-1` is still inside
  the `BACKFILL_DAYS` retention floor — exactly one extra request per series
  per cycle for half-hour zones (`3 + (7+1) + (7+1)` on a solar contract,
  pinned by `TestHalfHourZoneRequestCeiling`; the whole-hour `3 + 7 + 7`
  contrast by
  `TestComposedRequestCeiling.test_worst_case_disjoint_chunks_normal_cycle`),
  none for whole-hour zones. No
  overlap at the floor: a fresh half-hour-zone install loses the very first
  day's 00:00 slot — accepted, asserted by a test.
- **Trim is derived from batch CONTENT, never the plan**: after dedupe and
  bucketing, if the earliest fetched slot IS the contract-local 00:00 slot
  of a half-hour zone (`:30` past the UTC hour) the batch does not own its
  first bucket, and `_import_intervals` / `_import_generation` drop that
  first hourly bucket in EVERY series (aggregate, cost, each ToU band,
  generation, credit, marker rows) BEFORE the early return and
  `cutoff = min(hour_cons)`. Any OTHER `:30` first slot — a whole-hour
  zone, or a half-hour-zone day whose opening slots were zero-on-zero and
  parser-filtered (every generation batch; a meter reading exactly 0) — is
  an hh:00 local slot whose bucket the batch owns outright, so no trim
  (`_straddle_trim_before` compares against `_local_midnight_utc`, not the
  minute). Keying the trim on the planned overlap day would re-open the
  loss whenever the overlap fetch returned nothing usable (per-day error,
  all-placeholder day, big-gap resume past the floor).
- **Overlap-day errors are non-fatal** and never set `solar_skipped` — a
  heal sweep cannot be kept pending by a context-only day. With the content
  trim the stored straddle row is left intact (baseline row = the straddle
  hour, cutoff = the next full hour), so the chain stays monotone; the cost
  is that batch's own 00:00 half-slot. That slot is NOT restored by the
  next cycle's overlap — an overlap day's own 00:00 is always inside the
  trimmed first bucket — only by a later cycle that fetches the day as a
  RANGE day together with its predecessor. In the steady-state rewindow the
  previous cycle already wrote that bucket whole (both days were range days
  then), so the cost is one skipped refresh of a week-old bucket: nothing
  lost. During initial backfill / a big-gap chunk resume the chunk's
  first-day 00:00 half-slot is permanently missing (one 30-min slot per
  such error; the stored 23:30 half is intact; the chain stays monotone)
  unless the day later re-enters the trailing rewindow as a non-first day.
  Pinned on the real recorder by
  `tests/test_recorder_statistics.py::test_half_hour_zone_overlap_day_error_residual_is_one_half_slot`.
- **Stall tracking** counts `progressed` only from fetched days INSIDE the
  original un-overlapped range, and give-up markers never span the overlap
  day — otherwise a healthy overlap day would reset the #154 counter every
  cycle and re-fetch a permanently-erroring chunk forever.
- **Zero-export marker rows** start at `_first_full_hour(local midnight)`,
  never the floored straddle bucket — a marker at `14:00Z` would overwrite
  the previous day's 23:30 export. Identical to before for whole-hour zones.

**Stale-key fill (#300, every import)**: the #292 correction moves slots
between hourly keys, and the parser drops zero-on-zero slots, so a
corrected batch can carry NO value at an hourly key that still holds an
old-convention row. That row kept its old running sum, above its newly
written neighbour: a downward step (reproduced on the real recorder on
beta.3: SA 5 × 0.38 kWh, QLD-in-DST 4 × 0.78 kWh at `04:00Z`, the end of a
solar home's midday zero-import block; generation the same at night).
`_import_intervals` (aggregate, cost, every ToU band series) and
`_import_generation` (generation, credit) therefore make ONE extra batched
executor read per import (`_stored_hourly_states`, in the same
`asyncio.gather` as the baselines, stored `state` for every row at/after
the cutoff, unbounded above) and `_stale_key_fill` emits every stored key
`k >= cutoff` the batch has no value for:

- **0.0** only when every contract-local date `k` holds (`_key_days`: `k`
  and `k + 30 min`, so a half-hour zone's straddle bucket needs BOTH days)
  is authoritative: dated by >= 1 deduped READING (never a marker or a
  bucket) AND in `reading_days`, the set of days whose OWN fetch returned
  readings (`_fetch_sweep_day` / `_fetch_solar_day_into` provenance; a
  neighbour's trailing-slack row never vouches for a day; the #154
  give-up passes `()`; `None` — direct callers — trusts the readings'
  dates). Conservation guard (`_non_conserving_days`): a day that would
  lose stored kWh also needs batch kWh >= stored kWh less the stored kWh
  in its last local hour (the correction only moves readings later, so a
  pure re-key shifts at most the next day's opening slots into that
  hour); a failing day is de-authorised for the whole family and logs one
  bounded WARNING per import.
- **its stored state, carried forward** otherwise — the hour's kWh is
  unchanged and only its sum is re-chained, so an errored, empty or
  short day (and a stored row after the batch's last key) keeps its data
  while every row from the cutoff on becomes baseline + non-negative
  deltas.

Batch values always win; the fill never creates a key or a band series
that has no stored rows (`_emit_tariff_series` skips an empty band but
emits a band whose only rows are fill rows). Baselines are unchanged, the
`if not hour_cons: return` early exit still precedes everything (an empty
batch reads and writes nothing), and the steady state finds no stale key
and writes nothing extra. Active ONLY when `client.tz_is_contract is
True` (identity check): under the HA-timezone fallback local dates are
untrustworthy, so there is no read and no fill — imports behave exactly as
before #300. The rule is convention-agnostic: the beta.3 trailing week,
any future re-key, and a downgrade followed by a re-upgrade all self-heal
on the next poll. Pinned on the real recorder by the `test_300_*` tests in
`tests/test_recorder_statistics.py`.

### Previous Bill Period

```
GET /mobile/bff/api/v2/usage/smart/Electricity/{contractNumber}/Previous/Hourly?period=YYYY-MM-DD_YYYY-MM-DD&scaling=...
```

Used for backfill of dates **before** the current billing period start (`bill_period.start`).
Confirmed working back to at least 2025-12-24 (single-day period params). Requires the same
`Accept-Features`/`Client-Device`/`scaling` headers as `Current/Hourly`.

### Solar Generation (feed-in)

```
GET /mobile/bff/api/v2/usage/smart/ElectricitySolar/{contractNumber}/Current/Hourly?period=YYYY-MM-DD_YYYY-MM-DD&scaling=...
```

Documented from real captures provided on #128 (2026-07-03, plus a full-day
2026-07-01 capture with app reference figures). Same envelope, headers, and
`scaling` requirement as the `Electricity` endpoint — the path substitutes the
`ElectricitySolar` segment, `resourceType` comes back as `electricity-solar`,
and each item carries **both** a `consumption` block and a shape-identical
**`feedIn`** block:

- **Exported kWh**: `feedIn.quantity` (outer) — **CONFIRMED 2026-07-06**
  against the AGL app for the 2026-07-01 capture: `sum(outer feedIn.quantity)`
  = 8.019 kWh vs the app's "Sold to Grid 8.02 kWh"; `sum(outer feedIn.amount)`
  = $1.3629 vs the app's $1.36. The inner `feedIn.values.*` sums to 6.1448
  (the usual DPI/chart-scaled undercount) — do not read it. Regression test:
  `tests/test_parser.py::TestParseSolarIntervals::test_feedin_reconciles_with_agl_app_figures`.
- **Feed-in credit**: `feedIn.amount` (outer) — AUD credited for the slot.
- **`feedIn.type`**: same vocabulary as consumption INCLUDING ToU bands — the
  2026-07-01 capture carries `normal` and `peak` typed feedIn slots. Filter
  `none`/`pending` as usual. Zero-on-zero feedIn slots are *real* at night
  (no sun) but are still safe to drop — a zero delta never moves the sum.
- **`dateTime`** on solar items is Sydney-converted exactly like consumption
  (#292, see Data API) and goes through the same re-localisation —
  `async_get_solar_hourly` passes `expected_day`/`tz`/`tz_is_contract`. The
  2026-07-01 fixture is an eastern-winter capture of UNKNOWN state: its
  `14:00Z` first slot fits any eastern state in winter and an SA contract
  under the bug alike, so it evidences field selection only, never a
  timezone convention (`tests/fixtures/PROVENANCE.md`).
- **Contract discovery**: `accounts[].contracts[].hasSolar` in `/v3/overview`
  gates the feature (the overview also shows a "Sold To Grid" label pair on
  solar contracts). A `Previous/Hourly` variant is assumed symmetric with the
  consumption endpoint (unconfirmed against a real capture — the fetch loop
  tolerates per-day errors either way).
- The solar response's own `consumption` block is **ignored** at runtime but
  is now reconciled: its outer sums on the 2026-07-01 capture (6.072 kWh /
  $2.2537) match the app's consumption figures (6.07 / $2.25), i.e. it
  mirrors the `Electricity` endpoint. The aggregate consumption series still
  reads the proven `Electricity` endpoint.
- **Backfill is per-series** (beta.2): `_fetch_range` takes separate
  consumption and solar `(start, end) | None` ranges, each resolved from that
  series' own resume point. A contract that gains solar later (or upgrades
  into solar support) backfills generation from the 30-day floor without
  re-fetching consumption days; the app-matching bill-period sensors stay
  `unknown` until the generation series reaches the trailing rewindow.
- **Leading-hole heal** (beta.3, #128): beta.1 seeded the generation series
  from the *consumption* resume point, so a caught-up beta.1 upgrader got only
  the trailing `REWINDOW_DAYS` of solar — a permanent hole before that, which
  the per-series resume (keyed off the *last* row) never revisits. `_plan_solar_fetch`
  detects it (`_generation_needs_heal`: earliest stored row well past the floor)
  and re-imports the FULL `floor..yesterday` window in one contiguous batch so
  `_emit_series` rebuilds the whole cumulative chain from a correct baseline (a
  partial fill would step the sum down — #114 class). Progress is **persisted**
  in `entry.data[CONF_SOLAR_HEAL]` as a record `{state, floor, attempts}`, not
  inferred. The `floor` is **frozen** when the heal starts — the pending record
  is written BEFORE the multi-second fetch (in `_plan_solar_fetch`, via
  `_write_solar_heal`) so an HA restart mid-heal resumes the same window rather
  than recomputing the floor from a later `today` — and re-read from the pending
  record each retry, so it can't slide forward and drop the oldest day (Codex
  P2, passes 2 and 3). `_fetch_range` returns `False` if a 429
  halted it **or any solar day was skipped** by a transient AGL error
  (`_fetch_solar_day_into` → `"skip"`), so the heal stays `SOLAR_HEAL_PENDING`
  and retries the frozen window rather than declaring done with a hole (Codex
  P1). After `MAX_SOLAR_HEAL_ATTEMPTS` incomplete sweeps it gives up to
  `SOLAR_HEAL_DONE` so a permanently-erroring old day can't wedge the heal or
  re-sweep every poll (Codex P3; matches `_fetch_day_solar`'s accepted rare-hole
  tradeoff). Once `SOLAR_HEAL_DONE` the **leading-hole trigger never re-arms**;
  a **broken chain** (downward sum step frozen by a 429 on the give-up sweep)
  detected after done arms ONE bounded repair generation — fresh attempt
  budget, `repair: true` in the record, never re-arms once marked, lifetime
  sweeps hard-capped at 2x `MAX_SOLAR_HEAL_ATTEMPTS` (#153). Bill-period solar
  totals during a heal cycle: a COMPLETE sweep drains the recorder queue
  (`_recorder_drained`, bounded by `RECORDER_DRAIN_TIMEOUT`) and publishes the
  healed number the same cycle (#152); an incomplete sweep or drain timeout
  stays suppressed — a wrong number is worse than a blank one. Attempt
  accounting is exception-proof: `_fetch_with_heal_accounting` persists an
  attempt on ANY sweep exit (#151), and `AglClient` wraps transport/parse
  failures into `AGLError` so nothing escapes the family the catch sites
  expect. Written like the rotated refresh token — no reload listener fires.
- **Normal-path backfill give-up** (#154): a chunk where every attempted solar
  day errors (no 429 involved) counts toward `_track_solar_stall`; after
  `SOLAR_STALL_GIVE_UP_CYCLES` consecutive zero-progress cycles on the SAME
  chunk, zero-delta markers advance the resume past the span (WARNING logged).
  In-memory counter — restart resets it (conservative). Rate-limited sweeps
  and heal sweeps are excluded by design. Each give-up persists a span record
  to `entry.data[CONF_SOLAR_STALL_SPANS]` (bounded list, surfaced in
  diagnostics as `stall_give_up_spans`) and raises a persistent HA Repairs
  issue — the marker rows make coverage stats look healthy over the hole, so
  the span record is the only durable evidence (CO-16.4). Heal/repair
  give-ups likewise raise Repairs issues and mark the done record with
  `gave_up`/`attempts` so diagnostics can tell give-up from clean completion.
- The beta.1 "numbers don't match" report (#128) was a **window artifact** —
  a cumulative-since-backfill sensor compared against the app's
  billing-period tile — not a field bug. When validating against the app,
  compare the *period* sensors (or per-day Energy dashboard bars), never the
  cumulative totals.

### Bill Projection / the shared `additionalLabel` pair

`/v3/overview` gives each contract ONE free-text label/value pair —
`additionalLabel` + `additionalLabelValue` — and **reuses that slot for
different quantities**:

| Contract | `additionalLabel` | `additionalLabelValue` | Evidence |
|---|---|---|---|
| Plain electricity | `"Bill Projection"` | `"$139.15"` | **UNCONFIRMED** — from the anonymised fixture only; no real `/v3/overview` capture is committed |
| Solar (`hasSolar: true`) | `"Sold To Grid"` | `"+ $7.43"` | fixture matches the #128-era captures ("Sold To Grid" label pair documented under Solar Generation above) |

The exact "Bill Projection" wording is therefore an assumption. The keyword
match (`_projection_label`, substring "projection", case-insensitive) fails
SAFE if AGL's real label differs — the sensor stays `unknown`, no wrong
number — and the parser DEBUG-logs the unmatched label so a user can report
the real text. If a user reports the sensor still `unknown` after v0.5.0 on
a non-solar contract, ask for that DEBUG line and correct the keyword.

The pair is only meaningful read **together**. `parser._projection_label`
returns the value only when the label contains "projection"
(case-insensitive); anything else returns `""` and the sensor stays
`unknown`, never a confidently wrong number — the same discipline as
`_classify_tariff`.

**The usage-summary endpoint has never been observed to carry
`additionalLabelValue`** (not "definitively never returns it" — there is no
real capture to prove a negative; graded per review).
`/api/v2/usage/smart/Electricity/{contractNumber}?isRestricted=False`
showed no usable projection in any release v0.1.0–v0.4.0 (maintainer-
confirmed blank on a live account throughout). `parse_bill_period` still
parses the root key into `BillPeriod.projection_label`, but the coordinator
deliberately does NOT consume it: the summary carries no `additionalLabel`
to key on, so a fallback is unguardable — a solar contract's value would
bypass the label check and publish feed-in credit as the projection. This
was #253: the sensor read `unknown` in every release because the summary
was the only source wired up.
`tests/fixtures/bill_period_response.json` carries an
`additionalLabelValue` that was invented in #13 to make the test pass (all
fixtures are synthetic — `tests/fixtures/PROVENANCE.md`); it is not
evidence of API behaviour.

Solar contracts therefore have **no** bill projection available from this
endpoint. That is a documented limitation, not a bug.

### Contract fuel types

`/v3/overview` returns `accounts[].contracts[].type` as
`"electricityContract"` or `"gasContract"` — a vocabulary known only from
the anonymised fixtures; other values may exist in the wild, which is one
reason the filter below fails open. Every usage endpoint in
`AglClient` is hardcoded to the `Electricity` path segment, so only
electricity contracts are serviceable. `config_flow._serviceable_contracts`
filters the rest out before both the picker and the single-contract
auto-select fast path (#260), and **fails open**: a contract whose `type`
is empty or unrecognised is kept, because locking out a working install
over a renamed string is worse than the bug the filter fixes.

### Plan / Rates

```
GET /mobile/bff/api/v2/plan/energy/{contractNumber}
```

Returns `gstInclusiveRates` list with `c/kWh` and `c/day` entries. Supply charge
is a `c/day` entry with `title` containing "Supply charge".

**Solar feed-in tariff lives in `gstExclusiveRates`**, not `gstInclusiveRates`
(FiT is GST-free, so this is correct behaviour on AGL's side, not an
inconsistency): a `kind:"detail"`, `type:"c/kWh"` row with `title` containing
"feed-in"/"feed in" (confirmed from a real solar plan capture on #128;
fixture: `tests/fixtures/solar_plan_response.json`). `parse_plan` scans both
lists; a plan without a matching row leaves
`PlanRates.feed_in_rate_cents_per_kwh = None` and the rate sensor reads
`unavailable`.

**Time-of-Use rate mapping (heuristic — needs real-capture validation)**: AGL
does not return a machine `tariffType` field on plan rates. ToU bands are
inferred from the free-text `kind:"header"` row and the per-rate `title` via a
keyword match (`parser._classify_tariff`: `shoulder` → shoulder; `off peak`/
`off-peak`/`offpeak` → offpeak; then bare `peak` → peak; anything else → None,
so unmatched bands surface as `unavailable`, never a misleading `0.0`). The
**statistics split does NOT depend on this** — it is driven entirely by the
well-documented per-interval `consumption.type`. Only the per-tariff *rate
sensors* rely on the plan-text heuristic. `tests/fixtures/tou_plan_response.json`
is shape-extrapolated from `plan_response.json` (headers "Peak"/"Shoulder"/
"Off Peak"); validate against a real ToU plan capture and correct the heuristic
if AGL labels bands differently (tracked in #90).

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
(`haggle_pin_mismatch_<host>`, `const.PIN_MISMATCH_NOTIFICATION_ID`) — but the
request still succeeds. This keeps a legitimate AGL cert rotation from
bricking HACS users. `_check_pin` reads the stored pins LIVE from
`entry.data` on every call (not from setup-time locals), so once a flow has
re-pinned, the still-running old instance cannot raise a stale notice before
the reload unloads it. Each distinct mismatching fingerprint is reported
once per entry setup (#280) — the connector opens a new TLS connection per
poll, so a single rotation used to log a WARNING on every connection until
the user re-pinned; a different certificate is still reported, and a
reload/restart resets the memory.

**Re-pin and reauth (#275)** — until #275 the notice's "Reconfigure" did not
exist and reauth always aborted `already_configured`, so the only real
remediation was delete + re-add. Now:

- **Reconfigure** (`config_flow.async_step_reconfigure`, its own step +
  warning text) is the ONLY deliberate re-pin. It re-pins each host whose
  capture is non-empty — never writes `""` over a stored pin — and stores the
  new refresh token. A host's notice is dismissed only if every Haggle entry
  now stores an empty pin or the new capture for it (the id is per host,
  shared by all entries); if either capture came back empty it ends
  `reconfigure_pin_incomplete` and the warning stays.
- **Reauth** refreshes the token and fills only EMPTY pins; it never
  overwrites a stored one and never dismisses a notice. Reauth is
  system-triggered — an on-path attacker can provoke it with a 4xx on the
  token refresh — so a re-pinning reauth would launder the interception.
  Making reauth re-pin is a one-function change (`config_flow._pin_updates`)
  if the maintainer ever reverses this.
- While a Reconfigure flow is open, HA suppresses reauth for that entry
  (`async_start_reauth` returns early). If Reconfigure aborts or is
  abandoned while the token is dead, reload the entry or restart HA to get
  the reauth prompt back.

Empty stored values (`""`) mean "no pin yet" — the validator is a no-op.
Older entries created before this feature land in this state and upgrade on
the next Reconfigure or reauth.

**Why a connector subclass and not `resp.connection`?** aiohttp releases the
`Connection` back to its pool the moment a response is constructed, so
`resp.connection` (and `resp._protocol.transport`) are already `None` by the
time `async with session.get(...) as resp:` enters. The first cut of TOFU
pinning shipped with that bug — every live install was running with empty
SPKI strings (verified 2026-05-03) — until the connector subclass redesign.
Tests must use a real local TLS server (see `tests/test_pinning.py`); mocking
`resp.connection` will not catch this lifecycle issue.

### Contract timezone (`CONF_LOCAL_TZ`)

The CONTRACT's local zone — not the HA instance's — is the authority for the
interval window (#242/#266) and, since #292, for the Sydney-conversion
correction, so a wrong zone now means WRONG timestamps rather than a dropped
slot. It is persisted in `entry.data[CONF_LOCAL_TZ]` as an IANA key (`""` =
unknown), derived from the service address by `parser.tz_for_address`
(state-level keys; postcode 2880 → `Australia/Broken_Hill`). Chain of
custody:

1. **Config time**: written from the selected contract's address on both
   the single-contract fast path and the picker path.
2. **Migration** (`__init__.async_migrate_entry`, `MINOR_VERSION` 1 → 2;
   `VERSION` stays 1): derived from `entry.title`, which is the address on
   both creation paths. A user-renamed title (or the `AGL <contract>`
   fallback title) migrates to `""` — bounded residual: that entry runs on
   HA's zone, uncorrected and loud, until the first successful overview
   cycle persists the real key. The migration never returns `False`.
3. **Reauth AND Reconfigure** refresh it from the matched contract's address
   (a non-security value from the authoritative source) — this is what
   upgrades legacy hash-title entries at their next repair.
4. **Every overview cycle**: `_refresh_from_overview` refines the client's
   zone via `set_contract_tz` and persists the key when it differs (no
   reload listener fires — same pattern as the heal record).

`async_setup_entry` resolves `ZoneInfo(entry.data[CONF_LOCAL_TZ])` → HA's
zone → `None`, guarded so a hand-edited key degrades to the HA fallback with
a WARNING rather than failing setup. The fallback is announced ONCE per
setup by `AglClient` (WARNING naming the zone and that timestamps stay
uncorrected) and flagged `tz_is_contract=False`. The four coordinator
local-midnight sites (heal floor, `_earliest_stat_date`, the period-totals
cutoff, generation markers) use `_local_midnight_utc(day)` in the contract
zone with the same fallback (#268). Diagnostics (schema v3) expose
`coordinator.contract_timezone` (resolved runtime zone) and
`entry.data.local_tz` (persisted key) beside HA's `timezone` — the triple
needed to triage a #292-class report.

**Downgrade stays safe**: a minor-version bump is loaded unchanged by any
older build (HA refuses an entry only when its MAJOR version is newer than
the handler's); the older build simply ignores the extra key.

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
  - On `hasSolar` contracts additionally:
    `haggle:generation_<contract_number>` — exported kWh, `has_sum=True`,
    `unit_class="energy"` (add as a **"Return to grid"** source in the Energy
    dashboard) and `haggle:generation_credit_<contract_number>` — AUD,
    `unit_class=None`.
- **`unit_class="energy"` is required** on the consumption statistic for it to appear in
  the Energy dashboard's "add consumption source" picker. `unit_class=None` silently excludes
  it from the UI filter even though the data is in the DB.
- **Time-of-Use (ToU) per-tariff series**: on a contract whose interval data carries
  `consumption.type` values other than `normal` (i.e. `peak`/`offpeak`/`shoulder`), the
  coordinator ALSO writes one series per tariff type present, named band-distinctly:
  - `haggle:consumption_<tariff>_<contract_number>` — kWh, `unit_class="energy"`, `has_sum=True`
  - `haggle:cost_<tariff>_<contract_number>` — AUD, `unit_class=None`, `has_sum=True`

  where `<tariff> ∈ {peak, offpeak, shoulder, normal}`. The per-tariff series sum back to
  the aggregate (the `normal`/anytime band is included precisely so no kWh is lost). The
  aggregate series is always written too, for backward compatibility.
  - **Double-count warning**: a ToU user must add ONLY the per-tariff consumption series to
    the Energy dashboard, NOT the aggregate `haggle:consumption_<contract>` as well — adding
    both counts every kWh twice. Flat-rate users add only the aggregate (no per-tariff series
    exist for them). Each band uses a stable, band-labelled `StatisticMetaData.name`
    (`TARIFF_LABELS` in `const.py`) so the picker can tell them apart.
- Resume point: `get_last_statistics(hass, 1, stat_id, True, {"start", "sum"})` — returns
  the last-imported hour so incremental updates don't re-import already-stored rows.
- Each import call is idempotent: `(statistic_id, start)` updates in place.
- **Period-coverage attributes** (#214): the three "this period" device-card
  sensors (`consumption_period`, `generation_period`, `generation_period_credit`
  — NOT the `haggle:*` statistics above) expose `period_start`/`covered_from`
  (ISO date strings) via `extra_state_attributes` whenever they publish a
  value. `consumption_period` reads AGL's own bill-summary total, which is
  never locally truncated, so the two dates always match. The solar pair is
  computed locally in `_get_generation_period_totals`, and for a billing
  period longer than `BACKFILL_DAYS` (a quarterly bill) `covered_from` can be
  later than `period_start` — `_earliest_stat_date` reports the true earliest
  covered day, and a `truncated: true` attribute is added (omitted when
  `False`) so the known quarterly-bill under-coverage limitation is
  self-describing rather than silent.

---

## What NOT to Do

- **No `requests`** — always `aiohttp`. Blocking I/O in the event loop will freeze HA.
- **No blocking I/O in the coordinator** — `_async_update_data` must be fully async.
- **No OTP/portal flow** — auth is PKCE via the user's real browser, not portal scraping.
- **No hardcoded contract numbers** — they come from `/v3/overview` at config time.
- **No polling faster than 24 h for interval data** — AGL won't have newer
  data. `OPT_POLL_INTERVAL_HOURS` (#228) lets a user lengthen the cadence,
  never shorten it below the floor — enforced both by the options-flow
  `vol.Range` and a defensive clamp in `coordinator._clamped_poll_interval`.
- **Don't store `access_token` in `entry.data`** — it's transient (15 min).
  Persist only `refresh_token` to `entry.data`; keep `access_token` in memory only.
- **Don't use `async_add_executor_job`** for AGL API calls — they're already async.
- **Don't pass `access_token` to `AglAuth`** — `AglAuth.__init__` expects a `refresh_token`.
  Passing an `access_token` silently fails: `async_force_refresh` posts it as a refresh_token,
  Auth0 rejects it, and the contract number is never set → HTTP 404 on every data call.
  For one-shot calls with a bare bearer token (e.g. config flow), use a direct `aiohttp` GET.
- **Don't finish a reauth or reconfigure flow through `_async_create_entry` /
  `_abort_if_unique_id_configured()`.** With no updates that aborts
  `already_configured` and drops the new refresh token — reauth could never
  succeed in any release before #275 — and `async_create_entry` raises
  `HomeAssistantError` for those sources anyway. Update the existing entry
  with `async_update_reload_and_abort(entry, data_updates=…)`, and never
  build the written data from `entry.data` captured before the final
  synchronous step (e.g. in `async_step_reauth`/`async_step_reconfigure` or
  across an await): the coordinator writes heal/stall records and rotated
  tokens while the flow is open, and `data_updates=` merges onto the LIVE
  data. Match the entry's own contract by number in `entry.data`
  (`_match_existing_contract`, over ALL discovered contracts, failing closed
  when the stored account is set and the discovered one differs or is
  missing), not with `_abort_if_unique_id_mismatch` — legacy hash unique_ids
  are sha256 of a token that has since rotated. Never show the picker, never
  write unique_id/title/contract/account, never write an empty SPKI capture
  over a stored pin, and never let reauth overwrite an existing pin:
  Reconfigure is the only deliberate re-pin. Nothing after
  `async_update_reload_and_abort` may await or touch `entry.runtime_data` —
  the reload starts eagerly inside that call.
- **Don't revoke the superseded grant after reauth or Reconfigure (or add
  any revocation there) without first making `_persist_refresh_token`
  compare-and-swap.** The reload starts eagerly and unload closes the
  session before cancelling refresh tasks, so a rotation already in flight
  on the old grant (or a setup still running under `setup_lock`) can
  persist OLD' after the flow's write and before the new instance reads the
  token — the grant the flow just minted is then the orphaned one. That is
  harmless only while every chain stays valid; with revocation it stores a
  revoked token and locks the entry out. Auth0's revocation may also cover
  every token for the same user, client and audience, including the one
  just stored. Recorded as accepted risk I-5 in `docs/threat-model.md`.
- **Don't omit `Accept-Features` / `Client-Device` / `scaling`** — omitting any of these
  from Hourly or Daily usage requests returns HTTP 500 with no useful error body.
- **Don't set `unit_class=None` on the consumption statistic** — HA's Energy dashboard
  consumption picker filters by `unit_class="energy"`. `None` silently hides the statistic.
- **Don't add BOTH the aggregate and the per-tariff consumption series to the Energy
  dashboard for one ToU contract** — they overlap (per-tariff series are a partition of the
  aggregate), so adding both double-counts every kWh. The integration writes both for
  backward compatibility; the docs/CHANGELOG tell ToU users to add only the per-tariff
  series and flat-rate users to add only the aggregate. When adding a new per-tariff series,
  always emit the `normal`/anytime band too (`TOU_SERIES_TARIFFS`) so the partition is
  complete and no kWh silently vanishes from the breakdown.
- **Never add a diagnostics field without routing it through the scrub pass**
  (`diagnostics.py::_scrub`). Diagnostics files are attached to public GitHub
  issues — assume every field will be public. Account/contract numbers hide
  inside composite strings (statistic IDs, display names, `unique_id`), which
  is exactly what the final scrub pass exists to catch; the leak tests in
  `tests/test_diagnostics.py` serialize the whole payload and assert the raw
  values never appear. When the payload shape changes, bump
  `DIAGNOSTICS_SCHEMA_VERSION` and update `docs/diagnostics.md` in the same PR
  (the triage routine parses by that contract).
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
- **Don't use unbounded `float()` coercion on AGL response values**. Use
  `safe_float` from `agl/parser.py` — now the SINGLE implementation, imported
  by `coordinator.py` rather than duplicated (the two copies had already
  drifted: one returned `-0.0`, the other `0.0`) — so `inf`/`nan`/negative
  **and implausibly large** values can't reach
  `async_add_external_statistics` and corrupt the cumulative-sum series.
  "Finite" was never a sufficient bound (#241): `1e308` is finite and passed
  straight through, and `1e308 + 1e308` evaluates to `inf` with no exception,
  so two such readings in one hourly bucket produced exactly the non-finite
  `sum` the check existed to prevent. Values above `MAX_AGL_NUMERIC` are
  rejected to `0.0`, never clamped to the bound — a zero delta leaves the sum
  untouched, whereas a clamped `1e6` writes a permanent false spike.
- **Don't parse interval readings without telling the parser which day was
  requested — and pass the local timezone.** `parse_interval_readings` takes
  `expected_day` and `tz` (plus `tz_is_contract`, #292); every `AglClient`
  fetch site must pass them (#242, Codex P1 on PR #266 — `AglClient` gets
  `local_tz` from the persisted `CONF_LOCAL_TZ` at setup, HA's configured
  tz only as a flagged fallback, refined each overview cycle from the
  contract's service-address state via `parser.tz_for_address`: the
  CONTRACT's local day is the correct window and can differ from the HA
  instance's timezone). Without `expected_day`,
  `coordinator._import_intervals` derives its baseline cutoff as
  `min(hour_cons)` — purely from response content — so ONE interval carrying
  an old `dateTime` pins the cutoff before all real recorder history, the
  baseline resolves to `0.0` instead of the true multi-year sum, and the same
  import writes today's real hours on top of it: a large downward step in the
  `sum` column (#114 class, from a single crafted timestamp). With `tz` the
  window is the true UTC shape of the requested LOCAL day, with
  `INTERVAL_WINDOW_TRAILING_SLACK_HOURS` of TRAILING-only slack (AGL
  interprets `period=` in the contract's local timezone and returns
  `dateTime` as a Sydney-converted UTC label that the parser re-localises
  BEFORE this check (#292), so a single-day query spans two UTC dates; DST
  is handled by the tzinfo). Never add LEADING slack: the baseline cutoff is
  `min(hour_cons)`, so leading slack of any width re-admits the cutoff
  attack at that width, while a late row cannot lower the min (Codex
  pass-2 P1 on PR #266). The tz-less ±1-DATE fallback
  alone is NOT sufficient: it accepts every instant of the adjacent UTC date,
  so an injected `D-1T00:00Z` reading still dragged the cutoff ~14 h early —
  stored rows in that gap left out of the baseline but not re-emitted, a
  #114 downward step with no 1970-style absurdity to catch.
- **Don't treat AGL `dateTime` as the slot start in contract-local UTC, and
  don't widen the interval window to absorb the Sydney offset.** The value
  is the meter's LOCAL label converted through the response's `timeZone` —
  Australia/Sydney for every contract (#292): 30 min early for SA/Broken
  Hill all year, 1 h early for QLD while Sydney is on DST, exact only for
  NSW/VIC/TAS/ACT. Every published release wrote SA data 30 min early
  (escaped, sev:high); the v0.5.0 betas additionally DROPPED the first slots
  of every SA and QLD-in-DST day, because the uncorrected `14:00Z` sat
  before the contract-local midnight (the daily "Dropped N interval(s)
  outside the window" WARNING on #292). Correct it in the parser
  (`relocalise_agl_timestamp`, keyed on the response's `timeZone` with
  `AGL_API_TZ_KEY` as the fallback) BEFORE the window check; the correction
  is monotone later-only for every mapped zone, so the no-leading-slack
  invariant above holds unchanged, whereas leading slack would re-admit
  #242 at its own width. Three corollaries: (1) correct ONLY when the zone
  is address-derived (`tz_is_contract`) — under the HA-timezone fallback a
  mis-zoned entry (HA on UTC or overseas, a hash-title entry before its
  first overview) would otherwise be shifted by Sydney's full offset into a
  window where all 48 slots look plausible: silent instead of loud; (2) if
  the premise were ever wrong for a zone the failure is SILENT (corrected
  slots still fit the window, data lands late), which is why the parser
  emits one bounded DEBUG tripwire per parse and why the stable gate needs
  a positive confirmation from the reporter, not an absence of warnings;
  (3) never apply it to the Daily endpoint — its `dateTime` is a date label
  with a literal `00:00:00Z`, already the local day.
- **Don't import a batch whose first hourly bucket straddles the
  contract-local day boundary without the overlap day AND the
  content-derived trim.** In a half-hour zone (SA / Broken Hill / NT) local
  midnight is `:30` past the UTC hour, so the boundary bucket holds the
  previous day's 23:30 slot AND this day's 00:00 slot. A batch starting at
  day D rewrites that `(statistic_id, start)` row with only its own half,
  and the baseline is taken from the row before it, so the 23:30 slot
  leaves the sum chain permanently — on every sliding rewindow, chunk
  boundary and big-gap resume (reproduced on the real recorder: 96 → 95 kWh
  per sliding day). Fetch `S-1` as context, then drop the first bucket in
  EVERY series whenever the earliest fetched slot IS the contract-local
  00:00 slot (`:30` past the UTC hour) — keyed on batch content, never on
  whether the overlap day was planned or succeeded, and never on the bare
  minute: in a half-hour zone every hh:00 local slot is also `:30Z`, and a
  batch opening on one of those (the parser filtered the zero-on-zero
  slots before it — every generation batch) owns that bucket outright, so
  a minute-keyed trim dropped a real bucket for good at every floor / heal
  / big-gap start. Never write a zero-export marker at the floored midnight hour
  for the same reason (`_first_full_hour`), never let an overlap-day error
  mark a solar sweep incomplete, and never count the overlap day as stall
  progress. Whole-hour zones: no overlap, no trim, unchanged request budget.
- **Don't assume "no batch data at an hourly key" means "that key is
  untouched".** The parser's zero-on-zero filter drops slots, so a batch
  is silent at any hour whose slots all read zero. Any change that moves
  slots between hourly keys (#292's timestamp correction, a downgrade and
  re-upgrade, a future re-key) leaves stored rows at keys the new batch
  never writes, still carrying a running sum from the OLD chain, above
  their re-written neighbours: a #114-class downward step (#300, shipped
  in the withdrawn v0.5.0-beta.3). Every import must re-chain every stored
  key at/after its cutoff (`_stale_key_fill`). Never zero a key whose
  days the batch is not authoritative for — a per-day AGL error or a
  neighbour's trailing-slack row is not evidence that a day's stored kWh
  is gone; carry its stored state forward instead. Never let the fill
  create a key or a band series, and never arm it under the HA-timezone
  fallback (`tz_is_contract is True` only).
- **Don't "fix" a bare multi-type `except A, B:` by adding parentheses.** The
  unparenthesised form is intentional: it is `ruff format`'s canonical output
  for this repo's Python 3.14 target (PEP 758, where `except A, B:` means
  exactly `except (A, B):` and catches every listed type). Adding parentheses
  is not idempotent — `ruff format` strips them straight back out and the
  `ruff format --check` CI gate then fails. It is valid, correct 3.14 syntax,
  and HA 2026.7 requires Python 3.14.2 so it always runs. (An audit once
  compiled it under an older Python, saw a `SyntaxError`, and flagged it as a
  repo-breaking bug — it is not one; do not act on that.)
- **Don't forward raw AGL response dicts** via `dict(rate)` or similar
  open-schema passthrough. Allowlist exactly the fields the coordinator
  consumes, so a MITM-crafted response can't smuggle keys into runtime state.
- **Don't read kWh from `consumption.values.quantity` (inner).** That's a
  DPI/chart-scaled helper, not the meter read; in real captures `values.amount`
  always equals `values.quantity` and both undercount real consumption by
  4-73% with no consistent ratio. Read `consumption.quantity` (outer) for kWh
  and `consumption.amount` (outer) for AUD. Confirmed against the AGL portal
  "MyUsageData" CSV across 11 mitm captures, 2026-05-12. Regression test:
  `tests/test_parser.py::TestParseIntervalReadings::test_uses_outer_consumption_quantity_not_inner_values`.
- **Don't write `quantity == 0 && amount == 0` intervals to statistics.** AGL
  returns these as placeholders on days where the AEMO feed hasn't yet
  delivered the meter reads (with a non-`none` type, even). Inserting them
  creates phantom flat rows that the resume logic skips past forever once AGL
  backfills the real reads. `parse_interval_readings` filters them.
- **Don't read `additionalLabelValue` from `/v3/overview` positionally.**
  AGL reuses that one slot per contract for different quantities — "Bill
  Projection" on a plain contract, "Sold To Grid" on a solar one. Always
  gate on `additionalLabel` (`parser._projection_label`). A positional read
  publishes solar feed-in credit as the bill projection; and because
  `coordinator._money` strips the `+` from `"+ $7.43"`, the result is a
  *plausible* wrong number (7.43), not an obviously-broken one — the label
  key is the actual control, not the number formatting.
- **Don't trust `tests/fixtures/bill_period_response.json`'s
  `additionalLabelValue` as an API fact.** It was invented in #13 to make
  `test_projection_label_from_root` pass, one PR after #12 documented that
  the field is *not* in the usage summary. Every fixture except
  `solar_hourly_response.json` is synthetic (`tests/fixtures/PROVENANCE.md`)
  — a fixture is only evidence of API behaviour if PROVENANCE.md says it was
  captured.
- **Don't let the config flow offer a contract the client can't fetch.**
  All usage endpoints are `Electricity`-only; discovery returns gas
  contracts too. Filter through `_serviceable_contracts` before BOTH the
  picker and the `len(...) == 1` auto-select path — the fast path was the
  worse half of #260, silently selecting a gas contract on a gas-only
  account with no user choice and no error.
- **Don't put `AGL` (or any close variant) in `DeviceInfo.manufacturer`.**
  HA's "Service info" card renders `model by manufacturer`; this is an
  unofficial third-party integration and labelling the device as if AGL
  Energy authored it is misleading and a possible trademark concern. Keep
  `manufacturer="Haggle"`. AGL's name belongs only in `model`/docs as a
  factual description of the upstream service. Regression test:
  `tests/test_init.py::test_device_info_does_not_claim_agl_authorship`.
- **Don't pair `state_class=MEASUREMENT` with `device_class=MONETARY`.**
  HA validates this combination and logs a WARNING on every state update;
  only `None` or `TOTAL` are valid for MONETARY. Use `TOTAL` for cumulative
  cost-over-period, leave unset for one-shot forecasts.
- **Don't use `device_class=MONETARY` for unit prices.** MONETARY is for
  cumulative amounts (`$87.38 of cost so far`), not rates (`$0.34/kWh`).
  Pair the rate sensor with `state_class=MEASUREMENT` and a unit string
  like `"AUD/kWh"` instead — HA's price-tracking integrations (Nordpool,
  Tibber) follow the same pattern. Mixing MONETARY with no `state_class`
  *also* triggers HA's `state_class_removed` Repair if the entity ever
  reported stats under an earlier release.
- **Implement `async_remove_entry` if the integration creates entities.**
  Otherwise deleting the integration leaves orphan entity-registry rows
  whose `config_entry_id` references the now-gone entry; reinstall causes
  `_2`-suffixed sensor IDs that linger as `unavailable` forever. See
  `__init__.py::async_remove_entry`.
- **Don't clear the `haggle:*` external statistics in `async_remove_entry`.**
  Those rows are the user's own historical energy/cost data; deleting them on
  uninstall would silently and unrecoverably destroy years of Energy-dashboard
  history. Orphaned statistics are harmless and the user can prune them via
  Developer Tools → Statistics. Decided won't-implement on #91 (v0.3.2);
  `async_remove_entry` documents the deliberate omission. Do not add
  `async_clear_statistics` here without an explicit opt-in.
- **Don't fire backfill requests in a tight loop.** AGL's BFF will 429
  if 7 sequential GETs land in <1 s. `_fetch_range` sleeps
  `BACKFILL_INTER_REQUEST_DELAY` between days and breaks out of the chunk
  on `AGLRateLimitError`; the next 24 h cycle resumes from the gap.
- **Don't derive the cumulative-sum baseline cutoff from a `fetch_start` UTC
  midnight.** AGL's `period=YYYY-MM-DD_YYYY-MM-DD` query is interpreted in the
  contract's LOCAL timezone, so the first interval returned lands at local
  midnight in UTC (e.g. `(fetch_start - 1)T14:00Z` for AEST — `T14:30Z` for
  Adelaide after the #292 re-localisation, where the first bucket is a
  straddle bucket the content trim drops first). A baseline lookup
  cut off at `fetch_start T00:00Z` folds ~10 h of about-to-be-overwritten old
  sums into the baseline; the new chain re-adds those hours' deltas, producing
  a phantom `+N kWh` jump in the recorder `sum` column every local-midnight UTC
  row (visible on the Energy dashboard, which plots `sum[h] - sum[h-1]`).
  `_import_intervals` looks the baseline up — for the aggregate AND every
  per-tariff series — at the **actual earliest fetched-interval hour**, which
  is correct regardless of timezone or DST. This is why baselines are resolved
  AFTER the fetch (inside `_import_intervals`), not before it. Regression test:
  `tests/test_coordinator_statistics.py::TestImportIntervalsAggregation::test_baseline_looked_up_at_earliest_fetched_hour`.
  Fixed v0.3.0 → confirmed against the live recorder: 8 phantom spikes at
  `T14:00Z` (AEST local midnight) of 10–27 kWh each, including in the ToU
  `consumption_normal` series.
- **Don't track heal (or any multi-cycle repair) completion by inferring it
  from the statistics themselves.** The solar leading-hole heal first shipped
  "stateless" — re-detecting a leading hole / downward sum-step each cycle. Both
  proxies leak: a 429 that halts a heal after its markers reach the floor but
  before the real days complete leaves a monotonic-but-incomplete chain that
  neither proxy flags (permanent undercount), and a *legitimately* unfetchable
  leading gap (pre-solar days that HTTP-error rather than return empty) keeps the
  leading-hole check true forever → a 30-day fetch burst every poll. Track
  completion explicitly in `entry.data` and suppress bill-period totals during
  the heal cycle (the rewritten pre-`bill_start` rows are still queued in the
  recorder, so a live baseline read over-counts). Two more edge cases surfaced
  on the second Codex pass, both from the completion signal being too coarse:
  (a) recomputing the heal `floor` from `today` each retry slides it forward, so
  a 429-interrupted heal drops its oldest day — **freeze the floor** in the
  persisted record and re-read it on retry; (b) a day skipped by a transient
  non-429 AGL error (`_fetch_day_solar` → `None`) still let the sweep report
  "complete" — count **any** skipped solar day as incomplete so the heal
  retries, but **bound the retries** (`MAX_SOLAR_HEAL_ATTEMPTS`) so a
  permanently-erroring old date gives up gracefully instead of wedging pending
  forever. The persisted heal is therefore a record `{state, floor, attempts}`,
  not a bare state string. First pass: Codex P1/P2/P3; second pass: the
  floor-slide and skipped-day P2s — all on PR #150.
- **Don't let non-`AGLError` exception types escape `AglClient`.** Every
  coordinator catch site (`except AGLError`, the heal's attempt accounting,
  the failure-retry interval) is designed around the AGLError family. An
  unwrapped `aiohttp.ClientError`, `TimeoutError`, or `JSONDecodeError` from a
  200 non-JSON body (Akamai challenge page) crashes the whole cycle *before*
  any of that machinery runs — the red-team trace showed a deterministic one
  on an old heal-window day wedging an unbounded 30-day sweep every cycle,
  integration unavailable throughout (#151). `AglClient._get` and
  `async_force_refresh` wrap transport/parse failures into `AGLError` (a
  network blip during token refresh must be retryable `AGLError`, never
  `AGLAuthError` — it is not an auth failure and must not trigger reauth).
  When adding a new client method, route it through `_get` or replicate the
  shield; `_fetch_with_heal_accounting` is the belt-and-braces layer that
  counts an attempt on ANY sweep exit regardless.
- **Don't leave schema-trusting code outside the transport shield.** A
  `try` that catches only `_TRANSPORT_ERRORS`/`JSONDecodeError` around the
  `resp.json()` call does nothing for the `data["..."]` / `int(...)` /
  `datetime.fromtimestamp(...)` block *after* it. A 200 whose body is valid
  JSON of the wrong shape (`null`, `[]`, `{"expires_in": "x"}`) raises
  `AttributeError`/`KeyError`/`TypeError`/`ValueError`/`OverflowError`,
  which bypasses every coordinator catch site (all built around the
  `AGLError` family), skips the #155 retry cadence, and lands in
  `last_exception` → published diagnostics. Guard the shape explicitly
  (`isinstance(data, dict)`) and wrap the conversions, raising a *retryable*
  `AGLTransportError` — never `AGLAuthError`, which would burn a working
  grant on a reauth prompt for what is not an auth failure (#243).
- **Don't interpolate an AGL/Auth0 response *field* into an exception
  message either.** The "no raw bodies in exceptions" rule is usually read
  as being about `resp.text()`, but a single field is enough: `f"Token
  refresh error: {error}"` echoed a 500-character structured payload, and
  `int(hostile)` puts its input into the `ValueError` text. Both reach HA
  Persistent Notifications and `diagnostics.py`'s `str(last_exception)`,
  which users attach to public issues. Echo a length-capped, type-checked
  slug or the exception *type name* only (#243).
- **Don't hardcode release version strings in README/info.md/docs.** The
  release flow bumps `manifest.json` + `CHANGELOG.md` only, so a pinned
  `vX.Y.Z` anywhere else rots on the next release (the README advertised
  `beta.2` while `beta.4` was live). Use the shields.io release badge or
  "latest pre-release via HACS" phrasing; version numbers belong in the
  CHANGELOG and the releases page.
- **Don't commit the release version bump directly to `main`.** The
  `protect-main` ruleset (2026-07-12, #171) requires a PR + green status
  checks even for the repo owner, so the pre-ruleset release flow
  (`git commit` on main + `git push origin main --tags`) bounces. The
  ruleset-era flow: bump via a short-lived PR, then create a **signed** tag
  on the squash-merge commit (`git tag -s vX.Y.Z origin/main`) and push
  just the tag with the `HAGGLE_ALLOW_MAIN_PUSH=1` hook override — tag
  creation is not blocked by the rulesets (`protect-release-tags` blocks
  update/delete/force on `v*`, not creation). See
  `.claude/agents/release-manager.md` for the full sequence. Since
  2026-07-13 the `protect-main` ruleset ALSO requires signed commits —
  compatible with this flow because squash merges to `main` are
  GitHub-signed and release tags are signed locally (security@naanya.biz
  ed25519 key), but it means remote agent sessions (which can't hold the
  key) cannot land anything on `main` except via squash-merged PRs. The
  same rule binds third-party PRs: an unsigned contributor commit blocks
  the merge button even for a squash, so `CONTRIBUTING.md` makes signing
  mandatory (#286); the fallback is re-applying the change as a signed
  commit with the contributor preserved as author (first used on #281). If the
  requirement blocks a legitimate flow (the first Dependabot cycle is the
  watch item), roll it back PR-first via `.github/settings/`, never as a
  silent toggle.
- **Don't re-add a `hooks` block to the committed `.claude/settings.json`,
  and never commit `.claude/hooks.sha256`.** Claude Code loads hooks from
  settings files with no content verification, no cross-session approval,
  and live file-watching, so anything a branch checkout can modify must not
  wire hook execution (#245). The wiring lives in the untracked
  `settings.local.json` (installed from `.claude/hooks-wiring.json` by
  `./scripts/pin-hooks.sh`), and every hook verifies the scripts against
  the untracked TOFU pin store before executing. After ANY edit to
  `.claude/hooks/*.sh`, re-run `./scripts/pin-hooks.sh` (after reviewing
  the diff) or every hook fails closed with a BLOCKED message — that
  failure mode is the control working, not a bug. The wiring commands
  no-op in any checkout lacking `.claude/hooks-wiring.json`, so they are
  safe under the optional machine-global managed-settings deployment the
  script prints (the complete fix for checkout-time wiring substitution
  — threat-model §6).
- **Don't import `voluptuous` directly in `config_flow.py`.** HA 2026.10
  replaced it with `probatio` (API-compatible) and types `data_schema` as
  `probatio.Schema`, so a plain `voluptuous` import fails `mypy` against
  current HA while a plain `probatio` import breaks every install on HA <
  2026.10 (the runtime floor is 2026.7.0). Keep the try/except compat import
  until the floor reaches 2026.10, then drop the fallback (#294). The weekly
  `compat.yml` run is pytest-only and did not catch this type-level break.
- **Don't re-add the remote ruff/mypy pre-commit hooks**
  (`astral-sh/ruff-pre-commit`, `pre-commit/mirrors-mypy`). Those hooks run
  a SECOND copy of the toolchain that drifts from `uv.lock` (they had
  reached ruff v0.7.4 / mypy v1.13.0 against locked 0.15.20 / 2.2.0 —
  local commits and CI were linting with different tools). Ruff and mypy
  run via `uv run` (`language: system`) so `uv.lock` is the single version
  source and Dependabot maintains it (2026-07 dependency review).
- **Don't pin pre-commit hook revs to mutable tags.** `rev:` must be a
  frozen 40-char commit SHA with a `# frozen: vX.Y.Z` comment — same
  branch-poisoning logic as the GitHub Actions SHA-pin rule; hook repos are
  code executed on every dev machine. Refresh with
  `pre-commit autoupdate --freeze`.
- **Don't add third-party actions to privileged workflows.** `release.yml`
  is the only workflow with `contents: write` + `id-token: write`; it uses
  first-party actions and the runner's `gh` CLI only (the third-party
  release action was removed in the 2026-07 dependency review). Same
  review removed the write-only Codecov upload from `ci.yml` — don't
  re-add external telemetry vendors to CI without a consumer for their
  output.
- **Don't lower `--cov-fail-under` in ci.yml to make a PR pass, and don't
  raise `max-complexity` to absorb a new C901 offender.** Both floors are
  deliberate ratchet gates (secure-SDLC standard CO-17.2): coverage ratchets UP as the
  total rises; a new over-complexity function gets decomposed, not legalized.
  As of #187 the tree carries **no `C901` `noqa` exemptions** — the former
  `coordinator._fetch_range` exemption was retired by extracting its per-day
  loop body into `_fetch_sweep_day`. Keep it that way: decompose, don't noqa.
- **Don't change GitHub repo settings, rulesets, or Actions policy without a
  PR updating `.github/settings/` first.** The control plane is settings-as-code
  (secure-SDLC standard CO-9): PR the intended state into `.github/settings/`, merge,
  apply the change, then run `./scripts/export-settings.sh` and confirm the
  working tree stays clean. The weekly `settings-drift` workflow files an issue
  on any divergence. Break-glass changes are allowed but must be reconciled
  by PR before the next weekly run. Admin-only settings (merge methods,
  security toggles, Actions policy + selected-actions allowlist) are
  snapshot-only — refresh `repo-admin-snapshot.json` in the same PR whenever
  they change.
- **Don't add an options `update_listener`/reload-on-options to this
  integration.** The coordinator writes entry.data mid-cycle (token rotation,
  heal record, stall spans) and a reload listener would bounce the entry on
  every rotation. Options are read LIVE each cycle
  (`OPT_SOLAR_STATISTICS_ENABLED` in const.py documents the pattern).
- **Don't rename the `haggle.zip` release asset or the hacs.json
  `"filename"` key independently.** HACS resolves
  `releases/download/<tag>/<filename>` literally; a mismatch bricks HACS
  installs for that release. hacs.json is read at the installed tag, so the
  pair must be consistent within every tag. The tag-signature gate also
  means `.github/allowed_signers` must be updated in the same PR as any
  signing-key rotation, or releases stop cutting.
- **Don't exact-pin `pytest-homeassistant-custom-component` to a single
  patch.** Upstream releases near-daily, so an exact pin manufactures a
  guaranteed weekly Dependabot PR and has caused resolver deadlocks
  (#106, #120). Keep it a range (`<0.14`); `uv.lock` is the
  reproducibility authority. The same logic is why Dependabot **ignores
  `pytest`** (`dependabot.yml`): phcc exact-pins pytest internally
  (0.13.346 → `pytest==9.0.3`), so an independent pytest floor bump can
  never install anything different and a floor above phcc's pin deadlocks
  the whole grouped PR (#170). Don't remove that ignore rule, and don't
  bump the pytest floor by hand past phcc's internal pin.

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
fix: handle missing consumption.quantity
chore(release): v0.2.0
ci: add hacs workflow
```

Every commit MUST carry a provenance trailer naming the AI tool that
produced or co-authored it (`Co-Authored-By: <tool> <email>`, one line
per tool, any vendor) or declaring `AI-Assisted: none`. The
`require-provenance-trailer` pre-commit hook enforces this (#283). Example:

```bash
git commit -m "feat: implement token rotation persistence

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Provenance

The maintainer's code is generated by AI (Claude Code) and reviewed by the
human maintainer (@naanyabiz); third-party contributions declare their own AI
tooling, or none, per commit (`Co-Authored-By: <tool>` / `AI-Assisted: none`)
and per PR (template disclosure). Every commit therefore carries a provenance
trailer, whatever the vendor (#283).
The integration is built against the API responses returned to a legitimate AGL
customer using AGL's own mobile client endpoints. No proprietary AGL code is
included. Anonymised response shapes are mirrored under `tests/fixtures/`; the
full API contract is documented in the "AGL API — Key Facts" section above.

### AI toolchain

Every AI tool that touches this repository, and the human boundary around it:

| Tool | Role | Pinning / scope |
|---|---|---|
| **Claude Code (CLI)** | Interactive author of all product code, operating under the maintainer's identity | The CLI itself auto-updates (not pinnable). Its tool grants are governed by `.claude/settings.json` (committed — the durable policy record) plus a per-machine `.claude/settings.local.json` (gitignored). |
| **Claude Code subagents** | Domain + review agents invoked in-session (see Subagent Triggers above) | Model-pinned in `.claude/agents/*.md`: 7× `claude-sonnet-4-6`, 1× `claude-haiku-4-5-20251001` (`release-manager`). |
| **Codex (`chatgpt-codex-connector`)** | Cross-vendor PR reviewer | Invoked on substantive PRs. Reviews are advisory comments only — never a merge or approval authority, and not a required check. |
| **Codex Security CLI (`@openai/codex-security`)** | LLM-based vulnerability scanner — distinct product from the `chatgpt-codex-connector` PR reviewer above, despite the shared "Codex" name | Dev-workstation tool, not a repo dependency (not in `pyproject.toml`/`uv.lock`; personal global npm install). Two modes: (1) periodic full-repo `scan --mode deep` audit, run manually by the maintainer, output kept outside the repo (`--output-dir`), findings triaged into labelled GitHub issues; (2) an **opt-in, pre-push-only** local hook (`.pre-commit-config.yaml`, id `codex-security`) that scans the diff against `origin/main` before a push. Not wired into a bare `pre-commit install` — a contributor without OpenAI/ChatGPT access is never blocked. **Never** added to CI: would require a stored `OPENAI_API_KEY`/`CODEX_API_KEY` secret, violating the zero-standing-secrets invariant (SECURITY.md § Access Review). Read-only — reads the working tree/diff and calls OpenAI's API; no write access to the repo. |
| **Third-party contributor tooling** (any vendor) | Outside PRs may be AI-assisted with any tool — the first merged one (#281) was DeepSeek-drafted | Declared per commit (`Co-Authored-By: <tool>` or `AI-Assisted: none`, checked by the local commit-msg hook once the contributor has run `pre-commit install` — CONTRIBUTING.md § Dev loop; the PR-template disclosure and maintainer review are the control for contributors who have not) and per PR (template disclosure). No repo access beyond the PR itself; reviewed and merged by the maintainer under the same zero-bypass gate as everything else; unsigned commits are re-applied signed with the contributor preserved as author (CONTRIBUTING.md § Sign your commits). |
| **`haggle-triage` routine** | Scheduled daily triage of untrusted issues/PRs/attachments: comments, labels, Dependabot rollups, draft-fix PRs | Cron-only by design; fresh session per run; tool + Bash-prefix allowlist. Committed spec and prompt: [`docs/agents/triage-routine.md`](docs/agents/triage-routine.md). **Never** merges, pushes to `main`, tags, releases, or edits `release.yml`/`CODEOWNERS`/`LICENSE`/`NOTICE`/`SECURITY.md`. |

**Human-approved boundary.** Merging a PR and creating/pushing a release
tag always require a live human decision — never a standing agent grant.
In practice either the maintainer runs them personally, or an agent
session runs them (e.g. the `/release` flow's `gh pr merge --squash` and
tag-push steps in `release-manager.md`) and halts on the interactive
permission prompt for the human to approve each one — that prompt IS the
boundary. The committed `.claude/settings.json` grants no merge verb (`gh pr
merge` is deliberately absent from the allow-list), denies
`Bash(gh auth token*)` outright (blocking the direct print path — an
interpreter file-read of gh's own config remains possible and is
tamper-evident rather than prevented, per the honest bounds in
[docs/threat-model.md §6](docs/threat-model.md)), and `ask`-gates every `Edit`/`Write`/`MultiEdit` touching
`.claude/**` — tamper-resistant, not tamper-proof; see the honest bounds
in [docs/threat-model.md §6](docs/threat-model.md). Per-machine `ask`
rules add a live permission prompt on merge and on the tag-push override.
The enforced floor is server-side: the zero-bypass `protect-main` ruleset
(see `SECURITY.md § Gating Policy`).

**Grant-union re-assessment trigger.** Widening any agent's grants —
adding an allow entry to `.claude/settings.json` or
`.claude/settings.local.json`, or widening the triage routine's tool /
Bash-prefix allowlist in `docs/agents/triage-routine.md` — is a material
change that re-opens `SECURITY.md § AI development agents` and
`docs/threat-model.md § 6 (AI development agents)`. Update both in the
same change; do not accrete grants silently.
