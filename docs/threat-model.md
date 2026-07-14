# Threat Model — haggle

**Status**: living document. Re-reviewed at every minor/major release and on
any impact re-assessment trigger (§9). Changes land by PR like all code.
**Provenance**: originated as a point-in-time STRIDE assessment
(2026-05-02, commit `fea8ce1`); rewritten 2026-07 as the committed,
current-state model. The raw assessment pack (SBOMs, SCA/SAST/secrets scan
outputs) is retained offline and available to a security reporter on
request. Real customer identifiers present in the original are replaced
here by the repo's canonical placeholders (account `1234567890`, contract
`9999999999`).

---

## 1. System description

Haggle is a Home Assistant (HA) custom integration that pulls smart-meter
electricity data from AGL Energy's (Australia) undocumented mobile-app API
and feeds it into the HA Energy dashboard via recorder long-term
statistics. Distributed via HACS; runs entirely inside the user's own HA
process; ships **zero** third-party packages (`manifest.json`
`"requirements": []`).

Core data flow:

    HA user browser  →[PKCE callback URL paste]→  config_flow.py
    config_flow.py   →[POST /oauth/token, PKCE]→  AGL Auth0 (secure.agl.com.au)
    AGL Auth0        →[access + rotating refresh token]→  config_flow.py
    config_flow.py   →[persists refresh_token]→   HA config entry (.storage, plaintext JSON)
    AglAuth          →[refresh grant, JIT]→        AGL Auth0
    AglAuth          →[Bearer JWT, 15-min]→        AGL BFF (api.platform.agl.com.au)
    AGL BFF          →[JSON energy data]→          HaggleCoordinator (parser: total over arbitrary JSON)
    Coordinator      →[StatisticData rows]→        HA recorder (idempotent on (statistic_id, start))
    User (optional)  →[anonymised diagnostics JSON]→ public GitHub issue → daily AI triage routine

Attacker-relevant properties:

- Haggle impersonates the AGL iOS app (shared public `client_id` + header
  set in `const.py`); the refresh token is the long-lived credential and
  rotates on every use.
- Both AGL hosts are TLS-pinned Trust-On-First-Use by SPKI hash;
  **mismatch is warn-only by design** (a strict reject would brick users on
  legitimate cert rotation), so AGL response JSON is treated as
  attacker-influenceable and the parser is fuzzed to be total over
  arbitrary JSON.
- HA typically shares a LAN with IoT devices: LAN adjacency (L3) is a
  realistic attacker position.
- HACS distribution makes supply-chain compromise a multi-victim event —
  the one factor graded above the project's natural consequence class.

## 2. Data classification

| Class | Data | Where it may live | Requirements triggered |
|---|---|---|---|
| **A — secret** | AGL/Auth0 refresh token (rotating); access token (15-min JWT) | refresh token: `entry.data` only; access token: memory only | Never committed, logged, or serialized. Diagnostics redact it; leak tests assert it can never appear; secret scanning guards commits at three layers — pre-commit gitleaks, GitHub push protection, and a required full-history gitleaks CI gate with repo-specific rules (`.gitleaks.toml`: Auth0 refresh-token and real-AGL-identifier patterns, plus a scanner self-test canary — PR #184). Error bodies are stripped before exceptions propagate. |
| **B — personal** | account number, contract number, service address, residence-fingerprinting meter timeseries | the user's own HA instance (incl. entry title = address, threat I-3) | Never in the repo — fixtures use the canonical placeholders (`1234567890` / `9999999999` / `1 Sample Street SUBURB QLD 4000`), with one documented exception: `tests/fixtures/solar_hourly_response.json` is a real, identifier-free full-day capture of the maintainer's own meter, retained because its reconciliation against the AGL app's reference figures IS the regression evidence (a formal consent/provenance note is tracked as an open issue), and the CI gitleaks rules treat the real identifiers as secrets in the shapes they actually leak: keyed fields, usage-endpoint URL paths, and `haggle:*` statistic IDs. A bare 10-digit number outside those shapes is not detectable — the placeholder discipline remains the primary control. HMAC-anonymised in diagnostics (account/contract, including inside composite strings). Never in exception text (BFF URLs carry the contract number — strip before raising). |
| **C — operational** | usage figures, rates, tariff bands, timestamps, SPKI-presence booleans | diagnostics, statistics, logs | Permitted — this is the diagnostic payload and is not personally identifying on its own. |
| **D — public** | code, docs, CI config | repo | Normal review. |

Every new data field must be classified before it reaches logs, exceptions,
diagnostics, or fixtures; every diagnostics field must route through the
scrub pass (`diagnostics.py::_scrub`). Enforced by the leak tests in
`tests/test_diagnostics.py` and the `/pr` checklist.

## 3. Trust boundaries

### TB-1: AGL HTTPS API → HA coordinator
- **Controls**: TLS + TOFU SPKI pinning on both hosts (warn-only mismatch +
  persistent notification; documented remediation = Reconfigure re-pin);
  allowlist parsing (no open-schema dict passthrough); `_safe_float`
  clamping (finite, non-negative); parser totality fuzz-enforced weekly and
  on every PR (`fuzz.yml` — unconditional PR smoke run with cached corpus,
  a required check since PR #186).
- **Assumption to question**: warn-only pinning means a LAN MITM with a
  trusted CA still gets one poisoned session — hence the parser hardening.

### TB-2: HA user browser → config flow
- **Controls**: PKCE S256 (`secrets.token_bytes(32)`), 128-bit state nonce
  validated on the pasted callback.
- **Assumption to question**: no host/scheme validation on the pasted
  callback URL (threat S-3 — accepted defence-in-depth gap).

### TB-3: AGL JSON → HA recorder / statistics
- **Controls**: typed dataclass parsing, field allowlists, numeric guards,
  zero-on-zero and `type=none` filtering, idempotent imports.

### TB-4: GitHub supply chain → HACS installers
- **Controls** (verified against the live control plane 2026-07-13;
  SECURITY.md § Gating Policy is the authoritative list):
  - `protect-main` ruleset (active, `bypass_actors: []`, binds the owner):
    PR required; **eight** required status checks — Test (Python 3.14),
    Hassfest, HACS validation, Analyze (Python), CodeQL, Gitleaks (full
    history), Dependency review, Fuzz AGL parsers (atheris) — with
    strict up-to-date enforcement; signed commits required (newly enabled
    2026-07-13, under observation against bot-authored merges); squash-merge
    only. Classic branch protection is deleted — the ruleset is the single
    control source.
  - `protect-release-tags` ruleset (`v*` tags): update, deletion and
    non-fast-forward blocked — a published release tag cannot be silently
    repointed. Tag creation stays open (release flow).
  - Repository Actions policy: `allowed_actions: selected` (GitHub-owned
    plus four pinned publisher patterns) with SHA pinning required at the
    policy level; all `uses:` refs SHA-pinned (Dependabot-maintained); no
    third-party actions in the privileged release workflow; workflow-audit
    gates (actionlint, zizmor, shellcheck — PR #184).
  - Dependency-review gate on every PR (vulnerability severity + licence
    denylist, pinned-scope enforcement — PR #184).
  - Control plane as code: rulesets and repo settings are declared under
    `.github/settings/` and re-verified by a weekly settings-drift workflow
    that files an issue on divergence (PR #188); settings changes are
    PR-first.
  - Zero standing repo secrets; Sigstore-attested release artifacts; release
    tags signed (SSH ed25519, tagger `security@naanya.biz`, shows Verified
    on GitHub — tags cut before 2026-07-13 remain Unverified under the old
    tagger identity, accepted as historical fact); frozen-SHA pre-commit
    hooks.
- **Pending hardening (planned, not yet landed)**: zip-release-only HACS
  install path (`hacs.json` `zip_release`), per-release SBOM, SLSA level
  uplift. Do not cite these as in force until they merge.

### TB-5: HA diagnostics JSON → public GitHub issue → AI triage routine
- **From**: a user's HA instance (diagnostics download), attached by the
  user to a public GitHub issue. **To**: the public internet, and the daily
  automated triage routine that parses attachments (§6).
- **Controls**: the diagnostics file is built to be public — refresh token
  redacted, account/contract HMAC-anonymised per install (references
  correlate repeat reports but are not reversible), SPKI pins reduced to
  presence booleans, a final scrub pass over the serialized payload; leak
  tests serialize the whole payload and assert raw identifiers never appear
  (`tests/test_diagnostics.py`); `schema_version` gates machine parsing;
  the triage routine treats all attachment content as untrusted data.
- **Assumptions to question**: users may attach *other* files (raw HA logs)
  that are not scrubbed — the issue template asks for the diagnostics file
  specifically; a crafted "diagnostics" attachment is a prompt-injection
  vector against the triage routine — mitigations in §6.

## 4. Threat register and dispositions

19 threats from the 2026-05-02 STRIDE assessment, tracked to disposition.
"Accepted" rows are standing risk acceptances recorded in SECURITY.md's
risk-acceptance register (RA-14), accepted by @naanyabiz, 2026-07-13.

| ID | Threat (short) | Disposition | Evidence / rationale |
|---|---|---|---|
| S-1 | AGL endpoints MITM'd on the HA host's LAN (originally: "no certificate pinning") | **Mitigated; residual accepted** | TOFU SPKI pinning on both hosts (PRs #45/#48). Residual: warn-only mismatch + first-install pin capture — deliberate, documented in SECURITY.md; compensated by parser totality + fuzzing (#177, PR-gated since #186). |
| S-2 | Shared AGL iOS `client_id` detectable/revocable by AGL | **Accepted** | Fleet-wide availability dependency (see §8). Hot-update of client identity declined — it would require phone-home infrastructure worse than the risk. Recovery = coordinated re-release via HACS. |
| S-3 | Pasted callback URL not host/scheme-validated | **Accepted** (defence-in-depth gap) | Exploitation requires the state nonce (not externally exposed) and PKCE; impact is error-message quality (`config_flow.py::_extract_code` checks state only). May be closed opportunistically (reject URLs not starting `https://secure.agl.com.au/`). |
| T-1 | Crafted numerics (1e308 / negative / NaN) poison recorder statistics | **Mitigated** | `_safe_float` clamps to finite non-negative; parser total over arbitrary JSON, fuzz-enforced on every PR + weekly deep run (`fuzz.yml`, PRs #177/#186). |
| T-2 | Malicious GitHub Action via unpinned refs | **Mitigated** | All actions SHA-pinned with version comments (PR #42); repo Actions policy restricts to GitHub-owned + four pinned publisher patterns with SHA pinning required at policy level; actionlint/zizmor workflow-audit gates (PR #184); `continue-on-error` removed from HACS validation; no third-party actions in the privileged release workflow; Dependabot maintains pins; policy state snapshotted in `.github/settings/` with weekly drift detection (PR #188). |
| T-3 | Refresh-token prefix written to entity registry as `unique_id` | **Mitigated** | Fallback is `sha256(refresh_token)[:16]` (PR #43). |
| R-1 | No structured audit trail of token-rotation events | **Accepted** | Debug-level persist log exists; external use of a stolen token surfaces as a reauth event. A structured audit log is disproportionate — accepted; revisit if account-takeover reports appear. |
| R-2 | No disclosure path for reporters | **Mitigated** | SECURITY.md + GitHub private vulnerability reporting enabled (verified); CVD terms documented. |
| I-1 | Refresh token plaintext at rest in `.storage` | **Accepted (platform ceiling)** | HA offers integrations no vault API; the file is plaintext on ALL install types (HAOS included — corrected 2026-07). Compensating: 15-min memory-only access token; rotate-on-every-use refresh token; host FDE guidance in SECURITY.md §Storage. |
| I-2 | Token material in logged error bodies | **Mitigated** | Bodies stripped before exceptions propagate (AGENTS.md rule; regression tests, e.g. `test_force_refresh_redacts_body_from_exception`). |
| I-3 | Service address as entry title; contract number in statistic IDs — visible to all HA users of the instance | **Accepted** | Visible only to users the owner has admitted to their own HA instance; the entry title is user-renamable in HA; diagnostics exports anonymise both. Optional future: offer a display-name field in the config flow. |
| I-4 | `beautifulsoup4` dead dependency | **Resolved** | Removed; `manifest.json` ships `"requirements": []`. |
| D-1 | `client_id` revocation stops all installs; no backoff | **Accepted (with S-2)** / retry storms **mitigated** | Auth failures route to HA's reauth flow (no retry storm); failed polls retry at 30 min, restored to 24 h on success; 429s halt chunks without data loss. Availability residual accepted per §8. |
| D-2 | Rotated-token persist failure → lock-out on next restart | **Mitigated; residual accepted** | Persist failure now triggers **immediate reauth** (`__init__.py::_persist_refresh_token` → `entry.async_start_reauth`) instead of a silent time bomb. Residual: no two-phase persist — declined as disproportionate given the immediate-surface behaviour. |
| D-3 | First-install backfill burst triggers BFF rate-limiting | **Mitigated** | 0.5 s inter-request pacing, 7-day chunks, 429 halts the chunk and resumes next cycle (#34/#155); on the normal path a 429 never becomes a permanent hole. Within the bounded solar heal/stall give-up paths (§8), persistent rate-limiting counts toward the attempt caps and can end in a rare accepted hole. |
| E-1 | Compromised release executes in every installer's HA process | **Mitigated in depth; residual accepted** | Eight required checks under a zero-bypass ruleset (incl. CodeQL, full-history secret scan, dependency review, fuzz); required signed commits on `main`; Actions allowlist + SHA pinning; zero standing secrets; Sigstore-attested releases; signed release tags (`security@naanya.biz`) with a tag ruleset blocking mutation of published `v*` tags. Residuals (no independent reviewer; no HACS-side verification of what it installs) are RA-02/RA-08 in SECURITY.md. In force since 2026-07: HACS installs the attested zip itself (`zip_release`), per-release attested SBOMs, and fail-closed ancestry + tag-signature release gates. |
| E-2 | Open-schema `dict(rate)` passthrough into runtime state | **Mitigated** | Allowlist parsing; "don't forward raw AGL response dicts" is a standing AGENTS.md rule. |
| E-3 | Borrowed iOS `client_id` supports account-modification scopes the integration doesn't request | **Accepted with tripwire** | `AGL_OAUTH_SCOPE` contains no write scopes; **any change to the scope constant is an impact re-assessment + regulatory re-determination trigger** (§7, §9) and a mandatory security-review item. |

## 5. Residual-threat notes

The accepted rows above are deliberate, dated decisions — not omissions.
S-3, R-1, I-3, D-2-residual and E-3 are consolidated as RA-14 in
SECURITY.md's risk-acceptance register; I-1 (plaintext refresh token at
rest) is RA-05 and S-2/D-1 (shared `client_id`) is RA-06 (each accepted
by @naanyabiz, 2026-07-13), re-reviewed annually or when a second
maintainer joins.
Anything that changes an accepted threat's pre-conditions (new scope, new
endpoint, new storage location, telemetry) reopens the row.

## 6. AI development agents and Anthropic as a supplier

All product code is AI-authored (Claude Code) and human-reviewed/merged by
the maintainer. Two agentic systems operate **on the repo**; the shipped
product contains no AI component. Each is assessed on the **union of its
tool grants**, not by the (non-agentic) product's nominal consequence
class; widening any agent's grants re-opens this section.

**Interactive dev agent (Claude Code).** Operates under the maintainer's
identity with the committed policy in `.claude/settings.json` (narrow
allowlist, deny list, guard-main-branch hook) plus per-machine local
settings. Untrusted inputs it processes (its prompt-injection surface):
AGL API responses, GitHub issue/PR content, fetched web pages.
*Grant union (post-hardening, 2026-07-13)*: read/write to the working
tree; routine local git and feature-branch pushes; the build/test/lint
toolchain; read-only HA MCP tools. The per-machine allowlist was pruned
from ~136 to ~70 narrow entries; PR merge and the tag-push override
prefix are gated by `ask` rules so each fires a live human permission
prompt; printing the gh credential (`gh auth token`) is denied outright. (The maintainer's broader workstation and account
hygiene is operational practice outside this document's scope — this
section covers the agents' capabilities as they touch this repository.)
*Blast radius if hijacked*: the local checkout plus feature-branch
pushes. The permission layer (ask-rules on merge/tag and on
edits to the agent's own config under `.claude/`) is **tamper-resistant,
not tamper-proof**: the session holds general-purpose interpreters
(`python3`, `uv run`) whose file writes are not path-gated, so a
sufficiently deliberate injected session could rewrite its own
configuration — such edits are tamper-evident in `git diff`/session logs
rather than prevented. The *enforced* floor is server-side
and cryptographic: the `protect-main` ruleset binds any identity holding
the token (PR + eight green required checks, zero bypass actors,
squash-only, signed commits); merge and tag are human-gated
actions — the committed permission policy grants no `gh pr merge` verb
(narrowed 2026-07-14 after cross-review; previously `gh pr:*` was a
standing merge route on any fresh checkout), and the per-machine `ask`
rules add a live prompt on this machine. Every commit carries the
AI-provenance trailer (enforced by a local commit-msg hook — a
convention rather than a server-side control, backstopped by the PR
history and session links in every commit message).

**Automated triage routine.** A daily-cron-only hosted Claude agent
(deliberately *not* event-triggered — issue events would let attackers
summon it) that triages open issues and PRs: inventories, bundles
Dependabot PRs into one rollup PR, asks reporters for missing info, drafts
small fixes as PRs, and posts assessment comments on third-party PRs. It
never merges, never pushes to `main`, never tags or releases, and never
modifies `release.yml`, CODEOWNERS, LICENSE, NOTICE, or SECURITY.md. It
holds a GitHub credential while reading untrusted internet content — a
prompt-injection target by construction (lethal-trifecta review,
2026-07-06). Mitigations in force: fresh session per run (no
poisoned-memory carryover); untrusted-content armour (all
issue/PR/attachment content is data, never instructions; injection
attempts are labelled `possible-prompt-injection` and skipped); no
execution of code found in issue content; network confined to
github.com/api.github.com plus size-capped user-attachment downloads;
diagnostics attachments parsed strictly against the documented schema
(`docs/diagnostics.md`), undocumented fields ignored; per-run volume caps.
These prompt rules are defence-in-depth, not enforcement — the enforced
backstop is the branch ruleset plus the human merging everything. The
routine's committed record is
[docs/agents/triage-routine.md](agents/triage-routine.md) (repo-first
change control: the spec and prompt are edited there by PR — gated by the
injection-corpus replay in `docs/agents/injection-corpus.md` — and the live
platform-side definition is then synced from the merged file). Honest
residual: the platform copy remains editable outside version control, so
the sync is maintainer discipline, not an enforced control; if the two
ever disagree, the committed file is authoritative and the platform copy
must be re-synced from it.

**Anthropic as a supplier.** The models behind both agents (pinned model
IDs in `.claude/agents/*.md`) are a hosted supply-chain input: model
behaviour drift, service compromise, and verbatim training-data
reproduction are supplier-side risks this project cannot test for and
relies on Anthropic to control (risk-accepted — RA-11/RA-12 in
SECURITY.md). Cross-vendor AI review (OpenAI Codex PR reviews) is used on
substantive PRs as partial independence.

## 7. Regulatory scope — negative determination

**Determination (2026-07-13): no prescriptive regulatory development
regime attaches to this workload.** Haggle is read-only consumer
self-access to the user's own energy data using the user's own
credentials:

- **Not CDR** (Australian Consumer Data Right): haggle is not an
  Accredited Data Recipient and participates in no CDR data flows; the
  user accesses their own account via AGL's private mobile-app endpoints.
- **Not NER / AEMO / AER**: no market participation, no metering role, no
  demand response, no equipment control. The only AEMO reference in the
  project is a data-lag note.
- **No algorithmic-trading analogue**: nothing is traded, purchased, or
  bid.

Facts relied on (re-verify when reviewing this section): sensor-only
platform surface (`__init__.py` `PLATFORMS = [Platform.SENSOR]`), no
registered HA services, all non-GET HTTP confined to the two OAuth
token-endpoint calls, no write scopes in `AGL_OAUTH_SCOPE`, no actuating
features in the issue backlog.

**Reopening triggers** — this determination is void and must be redone
before merging any of: a write/account-modification OAuth scope; any
POST/PUT/PATCH/DELETE to AGL outside `/oauth/token`; any actuating HA
service (plan switching, load control, purchasing, VPP/market
participation); CDR accreditation; telemetry. (Same tripwires as the
impact re-assessment triggers in SECURITY.md.)

## 8. Resilience and recovery targets

Declared targets (each maps to a named constant in `const.py` and a
regression test in `tests/test_coordinator_statistics.py`):

- **Failed-poll retry**: a failed cycle retries in **30 min**
  (`RETRY_INTERVAL_ON_ERROR`), restored to the 24 h cadence on success;
  auth failures route to reauth, never fast retry.
- **Upstream outage**: an AGL outage of any practical length causes **no
  permanent data loss** — per-series resume points refetch forward from
  the last stored row, throttled to `BACKFILL_CHUNK_DAYS` (7) days per
  daily cycle with `BACKFILL_INTER_REQUEST_DELAY` (0.5 s) pacing (e.g. a
  14-day outage fully heals within two daily cycles of recovery).
- **Late upstream data**: AGL's AEMO-lagged backfills are absorbed for the
  trailing `REWINDOW_DAYS` (7): placeholder slots are overwritten with
  real reads on later cycles (imports are idempotent on
  `(statistic_id, start)`). Data arriving later than 7 days after its slot
  may leave a permanent hole — accepted.
- **Rate limiting**: a 429 halts the current chunk and resumes next cycle;
  on the normal path a 429 never produces a data hole. In the solar heal
  path, sweeps halted by persistent 429s count toward the bounded give-up
  caps below, so sustained rate-limiting can end in a rare accepted hole.
- **Bounded give-up**: the solar leading-hole heal is capped at
  `MAX_SOLAR_HEAL_ATTEMPTS` (3) sweeps (lifetime hard cap 2×); a
  persistently erroring span is abandoned after
  `SOLAR_STALL_GIVE_UP_CYCLES` (3) zero-progress cycles. Every give-up is
  user-visible: a persistent HA Repairs issue (ids key on the config
  entry's random id, never an identifier) plus durable records — the heal
  record's `gave_up`/`attempts` markers and the bounded
  `solar_stall_spans` list in the entry data (dates and counts only,
  Class C) — surfaced in diagnostics as `stall_give_up_spans`. Both
  produce rare permanent holes rather than wedged integrations —
  accepted, logged at
  WARNING (surfacing these as HA Repairs is tracked separately).
- **Restart survival**: multi-cycle repair state is persisted in
  `entry.data` (`{state, floor, attempts}`), and the rotated refresh token
  is persisted synchronously — an HA restart at any point loses neither.

**Dependency concentration**: every install shares AGL's iOS `client_id`
and the two AGL endpoints — a single systemic availability dependency with
no failover referent (there is nothing to fail over to). AGL-side
revocation would stop all installs simultaneously; recovery is a
coordinated re-release via HACS. Accepted (S-2/D-1; RA-06).

## 9. Review triggers and cadence

Re-assess this model: at every minor/major release (release-manager
checklist); on any impact re-assessment trigger (SECURITY.md §Impact
Assessment); on any new trust boundary (new outbound host, new inbound data
source, new agent); on widening any AI agent's tool grants (§6); annually
as part of the posture re-assessment, which includes re-reading Home
Assistant's current security guidance for integrations.
