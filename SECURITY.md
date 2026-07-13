# Security Policy

## Reporting a Vulnerability

Please report suspected security issues **privately** via either of:

1. **GitHub Private Security Advisories** —
   <https://github.com/NaanyaBiz/haggle/security/advisories/new>
   (preferred — keeps the reporter and the maintainer in a single thread).
2. **Email** — `security@naanya.biz`.

Please do **not** open a public issue for security reports.

We aim to acknowledge reports within **5 business days**. Response targets
are graduated by severity:

| Severity | Examples | Target |
|---|---|---|
| Critical, user-facing | refresh-token theft, RCE on the user's HA host, a malicious release | Mitigation or public GHSA advisory within **72 h** of confirmation; vulnerable release superseded (HACS delisting requested if no fix is possible) |
| High | auth bypass, PII disclosure in logs/diagnostics | Fix or mitigation within **7 days** |
| Moderate / Low, and dev-only lockfile CVEs | hardening gaps, CI-only dependency alerts | Within **30 days** or the next release train |

**Escalation**, for a single-maintainer project, means self-escalation with
a public forcing function: if a critical issue cannot be fixed inside its
window, the GHSA advisory is published anyway — without the fix — the
affected releases' notes are edited to warn users, and a pinned repository
notice is posted. There is no second responder (see the risk-acceptance
register below); the acknowledgement window is the honest bound on
maintainer absence.

## Supported Versions

The latest tagged release on `main` is the only supported version. Beta
tags (`vX.Y.Z-beta.N`) receive fixes; older `v0.0.x-dev` tags do not. There
is no LTS branch.

### End of life

If the project is retired or becomes unmaintained: (1) a final release and
README note will state the unmaintained status, (2) the repository will be
archived (HACS flags archived repos and blocks new installs), and (3) HACS
delisting will be requested. Installed copies keep working until AGL
changes their API — there is deliberately no phone-home to disable them
remotely.

### Known-vulnerable releases

If a released version turns out to carry a user-affecting vulnerability:
publish a GHSA advisory naming the affected tags → cut a superseding
release (HACS surfaces the update to users) → edit the affected releases'
notes to point at the advisory → if no fix is possible, request HACS
delisting. Pull-based distribution means installs can be neither
enumerated nor force-upgraded; publication is the only lever
(risk-accepted below).

## Scope

In scope:
- The Python code under `custom_components/haggle/` and `tests/`.
- The CI workflows under `.github/workflows/`.
- The documented HACS install path.

Out of scope:
- Issues in upstream Home Assistant or upstream `aiohttp`/`cryptography`
  (please report to those projects directly).
- Issues in AGL Energy's API itself — we are a client of an undocumented
  endpoint and have no privileged relationship with AGL.
- Vulnerabilities that require already-compromised local code execution
  on the HA host (the integration's threat model assumes the HA process
  is trusted).

## Impact Assessment

Data handled: one household's electricity interval/billing data plus the
user's own AGL OAuth refresh token, stored on the user's own Home
Assistant host; nothing is transmitted anywhere except to AGL's API.
Assessed on confidentiality / integrity / availability impact, the
integration itself is a **low-to-moderate impact** workload: worst-case
compromise of a single install affects one household's energy data and
that user's AGL session, on hardware they own. The one factor assessed at
**high impact** is supply-chain reach — a compromised release executes
inside every HACS installer's HA process — which is why the release and
CI integrity controls here are engineered well above what the package's
direct impact would justify. This classification is what the triage
judgements elsewhere in this document (e.g. "a green-CI dev bump is
zero-user-risk") rest on.

**Impact re-assessment triggers** — any of the following requires
re-validating this classification and the threat model before merge: a new
OAuth scope in `AGL_OAUTH_SCOPE`; any non-token-endpoint write call to
AGL; any new outbound host; any actuating HA service; any
credential-handling change; any form of telemetry.

**Re-assessment rhythm**: the threat model
([docs/threat-model.md](docs/threat-model.md)) is reviewed at every
minor/major release; a full posture re-assessment runs annually or before
any distribution-channel change (e.g. HACS default-store submission);
weekly automated measurement (OpenSSF Scorecard, CodeQL, Dependabot, the
settings-drift check) runs continuously in between. Annually, the OpenSSF
Best Practices badge answers are re-validated **and** Home Assistant's
published security guidance for integration developers is re-read against
the current code — the platform's expectations move, not just ours.

## Threat Model Summary

The full living threat model — trust boundaries (including the
diagnostics→public-issue boundary), the 19-threat STRIDE register with
per-threat dispositions, the AI development agents, the regulatory-scope
determination, and the resilience targets — is committed at
[docs/threat-model.md](docs/threat-model.md). It originated as a
point-in-time STRIDE assessment (2026-05-02) whose raw pack (SBOMs,
SCA/SAST/secrets outputs) is retained offline and available to a security
reporter on request. The short version:

### Trust boundaries

| Boundary | Notes |
|---|---|
| AGL HTTPS API → HA coordinator | TLS + Trust-On-First-Use SPKI pinning (see below). |
| HA user browser → config flow | OAuth state nonce + PKCE S256. |
| AGL JSON → HA recorder/statistics | Allowlist-style parsing; numeric values clamped to non-negative finite floats. |
| GitHub Actions → HACS installers | All Actions SHA-pinned; release artefacts attested via `actions/attest-build-provenance`. |
| HA diagnostics → public GitHub issue | Built to be public: refresh token redacted, account/contract HMAC-anonymised, final scrub pass + leak tests; parsed by the daily triage routine strictly as untrusted data. |

### Trust-On-First-Use TLS pinning

The integration captures the SHA-256 SPKI hash of `secure.agl.com.au`
and `api.platform.agl.com.au` during the initial PKCE config flow and
persists both to `entry.data`. Every subsequent token refresh and BFF
request observes the live SPKI and compares it to the stored value.

**Mismatch is warn-only**: a HA persistent notification fires
(`haggle_pin_mismatch_<host>`) and a WARNING is logged, but the request
still completes. This is deliberate — a strict-reject mode would brick
HACS users on legitimate AGL cert rotations. The documented remediation
is the standard HA Reconfigure flow on the integration card, which
re-pins both endpoints on success.

**First-install caveat**: a LAN MITM during the initial PKCE flow could
pin the attacker's certificate. PKCE happens in the user's browser
(system trust + visible lock indicator), so this requires compromising
both the browser and the HA host simultaneously.

### Storage

- The OAuth2 refresh token is persisted in HA's config-entry data
  (`.storage/core.config_entries`). **That file is plain-text JSON on
  every install type — including Home Assistant OS** — unless the host
  itself runs full-disk encryption. (An earlier version of this document
  claimed HAOS encrypts it at rest; that was wrong — HAOS has no default
  data-partition encryption.) Home Assistant offers integrations no vault
  API, so this is the platform ceiling; we accept it as a recorded risk
  (RA-05 below). Compensating controls: the access token is memory-only
  with a 15-minute expiry; the refresh token is rotated by Auth0 on every
  use, so a stolen copy is invalidated at the integration's next exchange;
  the fallback `unique_id` is a one-way hash, never token material. If the
  token file worries you, enable full-disk encryption on the HA host.
- The short-lived access token (15-minute expiry) is **never** persisted.
- The fallback `unique_id` (when contracts cannot be discovered at
  install time) is `sha256(refresh_token)[:16]`, not a token prefix.

### Supply chain

Posture last reviewed in the 2026-07 dependency review (branch
`claude/haggle-dependency-review-x2lupw`); the decisions below are
deliberate and should not be "tidied" without revisiting that review.

**Shipped runtime surface: zero packages.** `manifest.json` declares
`"requirements": []`. Everything the integration imports at runtime
(`aiohttp`, `voluptuous`, `cryptography`) is vendored and version-pinned
by Home Assistant core. Consequences:

- No pip package reaches a user's machine because of this repo; the
  user-facing attack surface is the integration code plus the two
  TOFU-pinned AGL endpoints.
- Dependency alerts and bumps are **dev-only** and are triaged on that
  basis — a green-CI dev bump is zero-user-risk, and a CVE in the
  lockfile is a developer-workstation/CI concern, not a user one.

**Dev lockfile.** `uv.lock` is hash-pinned (sha256 per artifact). It
resolves ~167 packages, ~90% of which are the Home Assistant ecosystem's
transitive tree (via `homeassistant` +
`pytest-homeassistant-custom-component`) — the unavoidable cost of
testing against real HA, and not trimmable from this side. The
heavyweights in it (`boto3`, `numpy`, `pillow`, `sqlalchemy`, `grpcio`,
the Bluetooth stack, `pyjwt`) are never imported by haggle code.

**Adopting a new dependency.** Before any new dependency is added — a dev
package, a GitHub Action, a pre-commit hook, and above all any runtime
requirement (which would break the zero-requirements invariant and is a
impact re-assessment trigger) — the pre-adoption gate is: verify registry
provenance (the package resolves to the source repository it claims to
come from; prefer signed or attested releases where the ecosystem offers
them), check maintenance health (recent activity, maintainer count,
OpenSSF Scorecard where published), and confirm licence compatibility
with Apache-2.0 (the CI dependency-review job enforces a copyleft
denylist on every lockfile diff). Record the outcome in the adopting PR;
a dependency that fails the gate needs a register entry, not a quiet
merge.

**CI / workflows.**

- All GitHub Actions are pinned to 40-character commit SHAs with
  `# vX.Y` comments (Dependabot keeps them current, grouped weekly).
- The repository Actions policy is allowlist-only: GitHub-owned actions
  plus four named publisher patterns (`astral-sh/setup-uv`,
  `hacs/action`, `home-assistant/actions`, `ossf/scorecard-action`), with
  SHA pinning additionally required platform-side — an unlisted or
  unpinned action cannot execute even if a workflow references one.
- Workflow-level permissions are read-only at the top level everywhere
  (`ci.yml` is `contents: read`; the validation/scan workflows are
  `read-all`); `release.yml` and `settings-drift.yml` start from
  `permissions: {}` and scope write grants to the single job that needs
  them (`release.yml`: `contents` + `id-token` + `attestations` write).
- **No third-party actions in workflows holding `contents: write`**: the
  GitHub Release is created with the first-party `gh` CLI, not a
  third-party action. The one third-party action with any write grant is
  `ossf/scorecard-action` (SHA-pinned) in scorecard.yml, which holds only
  scoped `security-events`/`id-token` writes for publishing its results —
  a documented, accepted exception.
- **No external coverage vendor**: the Codecov upload was removed
  (write-only output, third-party code on every PR).
- Every PR is gated by a `Dependency review` check: known-vulnerable
  dependency changes fail at **moderate** severity or above across all
  scopes, and copyleft licences (GPL/AGPL/SSPL family) are denied.
- `hacs/action` and `home-assistant/actions` are SHA-of-branch pins —
  upstream cuts no tagged releases. Accepted: they are the ecosystem's
  own validation gates, and the SHA still freezes the code.
- `ossf/scorecard-action`'s SHA pin freezes only the action *wrapper*;
  its `action.yml` executes `docker://ghcr.io/ossf/scorecard-action:<tag>`,
  a mutable container tag (Codex review on #168). Accepted: digest-pinning
  the image would bypass the supported wrapper and desync from Dependabot's
  SHA bumps; OSSF cosign-signs the image, and the job's write scopes are
  limited to SARIF upload + Scorecard's own OIDC result publishing.
- The HACS validation step runs without `continue-on-error`.
- Release tags trigger an attested GitHub Release via
  `actions/attest-build-provenance` (Sigstore-rooted; verifiable with
  `gh attestation`). Tags are human-cut and signed (ed25519) against the
  maintainer's registered signing key; tags cut before 2026-07-13 predate
  that identity and render as Unverified (RA-10).

**Secret scanning (layered).** GitHub push protection blocks
provider-pattern secrets at push time. The two highest-consequence leak
classes for this repo — an Auth0 rotating refresh token (the user's AGL
credential) and an un-anonymised AGL account/contract identifier — match
no provider pattern, so repo-specific rules in `.gitleaks.toml` (quoted
and unquoted token forms; camelCase API fields, snake_case persisted
keys, and usage-endpoint URL paths for identifiers) are layered on the
gitleaks defaults and run in two places: the pre-commit hook on every dev
machine, and the CI `Gitleaks (full history)` job on every PR, which
scans every commit in the repository with a checksum-pinned gitleaks
binary. That job fails closed: a scanner self-test must first detect a
seeded token, so a silently broken scanner cannot return a green verdict.

**Dev-machine hooks.** `.pre-commit-config.yaml` pins every remote hook
to a **frozen commit SHA** (refresh with `pre-commit autoupdate
--freeze`). `ruff` and `mypy` run via `uv run` (`language: system`) so
exactly one toolchain copy exists, versioned by `uv.lock` — the
tag-pinned mirror hooks previously drifted years behind the locked
versions. The pre-commit `gitleaks` hook is the local first line of the
layered secret scanning above; the CI full-history job is the
authoritative one.

**Monitoring.** CodeQL (weekly + per-PR, and a required PR check),
OpenSSF Scorecard (`scorecard.yml`, published results + README badge),
Dependabot on both ecosystems with grouped weekly PRs, and a weekly
`settings-drift` workflow watching the GitHub control plane itself. At
the 2026-07 review, direct dependencies measured 5.4–7.4 on OpenSSF
Scorecard (transitives up to 8.5); the weakest triaged link is `pyjwt`
(exactly pinned by HA core, sits in code paths this integration never
imports — JWT expiry is decoded with hand-rolled base64, not PyJWT).

**Own Scorecard posture.** Baseline self-assessment (Scorecard v5.3.0,
2026-07-12) was 7.0/10. After the same-day remediation batch — `protect-main`
ruleset (#171), atheris fuzzing (#173/#177), attested release `v0.4.0-beta.5`
(#174/#178), plus the OpenSSF Best Practices passing badge (#172,
[project 13582](https://www.bestpractices.dev/projects/13582)) — the
current run scores **7.5/10**, with 10s on Pinned-Dependencies, SAST,
CI-Tests, Dangerous-Workflow, Dependency-Update-Tool, Security-Policy,
License, Binary-Artifacts, Vulnerabilities, **Fuzzing** (0→10,
`PythonAtherisFuzzer integration found`) and **Token-Permissions** (9→10,
top-level `permissions: {}` on `release.yml`). Remaining state, triaged
(verification sweep in #179):

- `Signed-Releases: 8` (was -1) — every release now ships
  `haggle-<ver>.zip` plus its Sigstore bundle (`.zip.sigstore`); verified
  end-to-end with `gh attestation verify haggle-<ver>.zip --repo
  NaanyaBiz/haggle` (SLSA provenance v1, digest match, built from the
  release tag). Scorecard reserves 9–10 for provenance it recognizes by
  the `*.intoto.jsonl` filename convention; the `.zip.sigstore` bundle
  *is* SLSA provenance, so the residual 2 points are a naming artifact.
  **Accepted at 8.**
- `Branch-Protection: 3` (was -1 internal error) — the `protect-main`
  ruleset (#171, since extended to eight required checks + required
  signatures; see Gating Policy) is readable by the default workflow
  token (the fine-grained-PAT alternative stays rejected). Scoring above
  3 requires required human approvers / codeowner review / stale-review
  dismissal — the same team-structure wall as Code-Review. **Accepted at
  3** for a solo-maintained repo.
- `Maintained: 0` — pure repo-age gate (<90 days); self-resolves ~2026-08.
- `Code-Review: 0`, `Contributors: 0` — measure team structure (a second
  human reviewer; multi-organization contributors). **Accepted at 0** for
  a solo-maintained repo; bot reviews (Codex/Claude) are excluded by
  Scorecard's design and self-review would be theater.
- `Packaging: -1` — means "no PyPI/registry publish workflow"; not
  applicable to a HACS-distributed integration. Inconclusive checks are
  excluded from the aggregate. Accepted.
- `CII-Best-Practices: 5` (was 0) — passing badge earned 2026-07-12 at
  100% of criteria (all 66 answered with evidence). **Accepted at 5**:
  silver requires further process docs and gold requires ≥2 maintainers —
  the same team-structure wall.

Realistic ceiling ≈ 8.5–9 once the Maintained age gate expires (~2026-08):
the residual gap is the team-structure checks above (Code-Review,
Contributors, Branch-Protection's review tiers), which a single-maintainer
project cannot honestly score on. Each "Accepted at N" above is a standing
risk acceptance by @naanyabiz (2026-07-13), re-reviewed annually or on
adding a maintainer.

**Accepted risks (diminishing-returns line).**
`pytest-homeassistant-custom-component` is a single-maintainer package
tracked as a range (`<0.14`) — vendoring it would mean self-maintaining
a fork of HA's test harness forever. The HA transitive tree is accepted
as-is. Detection tooling (CodeQL, gitleaks, Scorecard) is kept even
though each adds a component: it is detection surface, not attack
surface.

### AI development agents

100% of this repo's code is AI-authored (Claude Code) and human-reviewed
by the maintainer; two agents operate **on** the repository and none ship
in the product. Each is assessed on the union of its tool grants, not the
(non-agentic) product's nominal consequence class, and widening any
agent's grants re-opens the analysis. The full treatment — inputs,
injection scenarios, containment, and Anthropic as a hosted model
supplier — lives in the threat model
([docs/threat-model.md](docs/threat-model.md), §AI development agents).
The enforced backstop for both agents is structural, not prompt-based:
the zero-bypass `protect-main` ruleset plus a human executing every
merge, tag, and release.

| Agent | Untrusted inputs | Grant union | Blast radius if hijacked |
|---|---|---|---|
| Interactive dev agent (Claude Code, under the maintainer's identity) | AGL API responses; GitHub issue/PR content it reads; fetched web pages | Working-tree read/write; routine local git + feature-branch push; the build/test/lint toolchain. No standing grant to merge PRs, push to `main`, push tags, release, or reach remote hosts — each forces a live human prompt; reading the `gh` auth token is denied outright. | The local checkout plus feature branches. It cannot self-merge, self-release, or reach infrastructure beyond the repo without a human approving; direct `main` pushes are server-rejected by the ruleset. |
| Automated triage routine (`haggle-triage`, daily-cron hosted agent — spec and prompt committed at [docs/agents/triage-routine.md](docs/agents/triage-routine.md)) | All issue/PR/comment/diff/attachment content | Cron-only (deliberately not event-triggered — issue events would let attackers summon it); fresh session per run; comments, labels, and PR branches only. It never merges, never pushes to `main`, never tags or releases, and never modifies `release.yml`, CODEOWNERS, LICENSE, NOTICE, or this file. | Spam/noise on this repo's issues and PRs; a hijacked run is bounded by the zero-bypass ruleset and the human-gated merge/tag/release boundary. |

The triage routine's configuration and prompt are under repo-first
change control: [docs/agents/triage-routine.md](docs/agents/triage-routine.md)
is the authoritative record, edited by PR, and the live routine is synced
from the merged file. The pre-change control on any prompt edit is a
manual replay of the injection corpus
([docs/agents/injection-corpus.md](docs/agents/injection-corpus.md)) — every payload must
be labelled `possible-prompt-injection` and produce no other action
before the change lands. Manual by necessity: the routine runs on a
hosted platform CI cannot exercise, so a CI gate here would be theatre.

AI IP-contamination (verbatim training-data reproduction reaching a
merge) has no in-repo detector and is a recorded acceptance — RA-11
below.

## Gating Policy

**What blocks a merge to `main`** (server-side `protect-main` ruleset,
zero bypass actors — it binds the owner too; the coexisting classic
branch protection was deleted 2026-07-13, so the ruleset is the single
control source, versioned at `.github/settings/ruleset-protect-main.json`):
a pull request is required (squash merge only), the branch must be up to
date with `main`, commits must be signed (squash merges are
GitHub-signed; the signature rule was enabled 2026-07-13 and is under
observation against the next automated Dependabot cycle — it will be
rolled back by PR if it blocks automation), and all **eight** required
status checks must pass — `Test (Python 3.14)` (ruff, mypy, the
shellcheck/actionlint/zizmor workflow audit, and pytest with the
ratcheting coverage floor), `Hassfest`, `HACS validation`,
`Analyze (Python)` and `CodeQL` (SAST), `Gitleaks (full history)`,
`Dependency review`, and `Fuzz AGL parsers (atheris)`. Branch creation,
deletion, and force-pushes on `main` are ruleset-blocked. GitHub push
protection blocks provider-pattern secrets at push time. Client-side,
pre-commit blocks ruff / mypy / gitleaks / conventional-commit
violations; the hook layer is bypassable by construction (`--no-verify`),
so the CI-side checks are the authoritative gate.

**Release tags** (`v*`) are covered by a second ruleset
(`protect-release-tags`): once a release tag exists it cannot be updated,
deleted, or force-moved; creation stays open to the human-executed,
signed-tag release flow. Known gaps, remediation in flight: the release
workflow does not yet verify that a new tag points at a commit on `main`,
nor that the tag is signed — a tag on an unchecked commit, or an unsigned
tag, would still build and attest. Both gates (`git merge-base
--is-ancestor` ancestry check and tag-signature verification) land with
the release-chain work package (SDLC remediation WP4).

**The control plane itself is versioned**: rulesets, public repo
settings, and the Actions policy snapshot live in `.github/settings/`;
changes land by PR first. The weekly `settings-drift` workflow re-exports
and diffs what the unprivileged workflow token can read — rulesets and
public repo settings; the admin-only snapshot (merge methods, security
toggles, Actions policy and its allowlist) is manual-refresh only, so it
drifts silently between exports — detection, not prevention, and only for
the covered surface (RA-09).

**What alerts rather than blocks**: CodeQL alerts, Dependabot alerts,
Scorecard regressions, settings-drift issues, and scheduled deep-fuzz
findings are triaged on the severity ladder above (critical 72 h /
high 7 d / rest 30 d) and tracked as labelled issues (`sev:*`,
`P1`–`P3`). Dev-only lockfile CVEs are triaged as zero-user-risk per the
supply-chain section — the manifest ships no requirements.

**Exceptions** to any gate are recorded — as dated entries in the
risk-acceptance register below or as "Accepted" notes in the section they
belong to — never applied silently.

### Pre-release canary

Every release runs as a beta on the maintainer's own production HA
instance against the live AGL API before a stable tag is cut. That
dogfood instance is the project's dynamic-testing analogue; the
hostile-input analogue runs continuously as atheris fuzzing of the parser
boundary (`fuzz.yml` — an unconditional smoke run on every PR plus a
weekly deep run with a persisted corpus).

## Credential Exposure Response

If a secret (a refresh token, a real API capture) ever lands in a commit:

1. Treat it as compromised immediately — do not wait for evidence of use.
2. Revoke: re-run the integration's Reconfigure/PKCE flow (Auth0's
   rotation invalidates the leaked refresh token); anything else, revoke
   at its issuer.
3. Rewrite the affected history before pushing; if it already reached the
   public repo, rewrite anyway and treat the value as public forever.
4. Re-scan full history (`gitleaks git .`) and require zero findings.
5. If any user could be affected, publish a GHSA advisory.

## Evidence & Records Retention

- **Permanent, tamper-evident**: git history, PR/merge records, release
  tags, Sigstore build attestations (GitHub attestation store + the public
  Rekor transparency log), GHSA advisories.
- **GitHub-lifetime**: check-run *conclusions* (queryable per commit via
  the API).
- **90 days only**: Actions run *logs* (step-level output). Accepted:
  conclusions and attestations are the durable record; re-running a check
  is the recovery path for expired detail.
- **No native audit store** exists for GitHub personal-account settings
  changes (ruleset edits, Actions policy). Compensated since 2026-07-13
  by settings-as-code: the declared state is versioned in
  `.github/settings/` and the weekly drift workflow files an issue on
  divergence — detection, not prevention (RA-09).

## Access Review (quarterly)

Run `./scripts/access-review.sh` — read-only, uses the maintainer's local
`gh` auth, exits non-zero on drift from the expected access surface:

- collaborators == exactly the maintainer (admin); deploy keys == 0;
  webhooks == 0
- Actions secrets == 0; Dependabot secrets == 0; environments == 0
  (the zero-standing-secrets invariant)
- the `protect-main` ruleset is still active with an empty bypass list

Then complete the manual half — these are not queryable with the CLI's
token, by design: confirm account MFA is still enforced, review authorized
OAuth apps and GitHub App installations
(Settings → Applications), and prune stale personal access tokens.

This review is deliberately **not** automated in CI: the deploy-key and
webhook endpoints require admin scope, and parking an admin PAT in Actions
would break the zero-standing-secrets invariant the review exists to
protect.

## Risk-Acceptance Register

Standing, dated entries. The same person authors, triages, and accepts
every exception in this project — **self-acceptance is the operating
model** of a single-maintainer repository, stated here plainly rather than
simulated away. Rows are appended or amended by PR, never silently edited;
merging the PR that adds or amends a row constitutes the acceptor's
signature. Each row is re-reviewed annually, on adding a maintainer, or
when its pre-conditions change. Rows whose remediation is still pending
are **Open**, not Accepted; subsequent remediation work appends or amends
its own rows.

| ID | Risk | Compensating controls | Status | Re-review |
|---|---|---|---|---|
| RA-01 | Single-party governance: no second-line review, no independent exception acceptance, no governance recipient distinct from the author. | External automated assurance (weekly Scorecard, CodeQL, OpenSSF Best Practices), public register, public repo. | Accepted — @naanyabiz, 2026-07-13 | Annually / on second maintainer |
| RA-02 | No independent human code review; author = approver = deployer for every change and release (Scorecard Code-Review 0, Contributors 0, Branch-Protection capped at 3). | Zero-bypass ruleset + eight required checks; cross-vendor AI review (Codex) on substantive PRs; attested releases; pull-based distribution. | Accepted — @naanyabiz, 2026-07-13 | Annually / on second maintainer |
| RA-03 | No independent security testing (human pen test / second-party red team), before first release or since. | Pre-release AI assessment pack (2026-05); continuous CodeQL + fuzzing + Scorecard; cross-vendor AI review as partial independence. | Accepted — @naanyabiz, 2026-07-13 | Annually |
| RA-04 | Bus factor 1: if the maintainer disappears, triage/releases/security response stop until someone forks. | Public Apache-2.0 licence; AGENTS.md as succession document; enforced documentation checklist; attested releases. | Accepted — @naanyabiz, 2026-07-13 | Annually |
| RA-05 | User's refresh token is plaintext at rest on the HA host (platform ceiling — HA has no vault API for integrations; HAOS does not encrypt the data partition). | 15-min memory-only access token; rotate-on-every-use refresh token; hash-only `unique_id`; host-FDE guidance. | Accepted — @naanyabiz, 2026-07-13 | On HA platform change |
| RA-06 | Shared AGL iOS `client_id`: single systemic availability dependency; AGL revocation stops every install at once; identity hot-update declined (would require phone-home). | Failures route to reauth (no retry storms); 30-min failure retry; recovery = coordinated re-release via HACS. | Accepted — @naanyabiz, 2026-07-13 | Annually / on AGL contact |
| RA-07 | No telemetry into deployed instances: vulnerable installs cannot be enumerated, fleet health cannot be observed, time-to-restore cannot be measured, rollback cannot be automated. Deliberate privacy stance — phone-home from an energy integration would be worse than the risk it measures. | HACS update surfacing; GHSA publication; user-volunteered anonymised diagnostics. | Accepted — @naanyabiz, 2026-07-13 | Annually |
| RA-08 | No admission control on the install path: HACS installs an unverified source snapshot and HA loads `custom_components/` without integrity checks (upstream capability gap). | Attested release artefact + documented `gh attestation verify` path; tag-integrity ruleset; protected release pipeline. | **Open** — a planned release-chain change (HACS installing the attested release zip directly) narrows the HACS half and is not yet landed; the HA-loader half is accepted as an upstream ceiling. Amend this row when the release-chain work merges. | On HACS/HA capability change |
| RA-09 | GitHub personal-account plane: no audit log of settings/ruleset changes; the sole admin can alter protections (enforcement-against-the-enforcer is impossible). | Settings-as-code baseline in `.github/settings/` + weekly drift workflow (detects, cannot prevent); weekly Scorecard re-measures branch protection; quarterly access-review script asserts the surface; ruleset readable by any workflow token. | Accepted — @naanyabiz, 2026-07-13 | Annually / on org migration |
| RA-10 | Historical evidence gaps: Actions run logs expire at 90 days; history before 2026-07-12 predates the zero-bypass ruleset; releases ≤ v0.4.0-beta.4 are unattested; 19 early direct-push commits are unsigned; tags cut before 2026-07-13 predate the registered signing identity and render as Unverified. | Durable evidence (attestations, signed tags, PR trail) covers everything going forward; superseded releases are out of support. | Accepted — @naanyabiz, 2026-07-13 | Never (historical facts) |
| RA-11 | AI IP-contamination: no verbatim-training-data detector or licence-similarity scanner gates merges. | Provider-side (Anthropic) mitigations relied on; purpose-built code against an undocumented API — low verbatim likelihood. | Accepted — @naanyabiz, 2026-07-13 | Annually |
| RA-12 | AI "double-key" authorisation is impossible solo: agent use-case approval and SDLC change control are held by the same person. | Documented AI toolchain; committed agent policy; human-executed merge/tag. | Accepted — @naanyabiz, 2026-07-13 | On second maintainer |
| RA-13 | No formal secure-development training regime for the maintainer. | OpenSSF Best Practices attestations; the AGENTS.md footgun corpus (recurring by construction — every PR appends new footguns); annual badge re-validation + HA security-guidance re-read. | Accepted — @naanyabiz, 2026-07-13 | Annually |
| RA-14 | Residual STRIDE threats accepted per the threat-model register: I-3 (service address as entry title), R-1 (no token-rotation audit trail), D-2 residual (no two-phase persist), E-3 (borrowed client_id supports elevated scopes), S-3 (no callback-host check). | Per-threat rationale + tripwires in [docs/threat-model.md](docs/threat-model.md) §4–5. | Accepted — @naanyabiz, 2026-07-13 | Annually / on trigger |
| RA-15 | (Pointer) The dated acceptances embedded elsewhere in this file — Scorecard "Accepted at N" scores, the scorecard-action mutable container tag, hacs/hassfest branch-SHA pins, the phcc range pin and HA transitive tree, warn-only SPKI pin mismatch — are standing acceptances on the same terms as this register. | See the respective sections. | Accepted — @naanyabiz, 2026-07-13 | Annually |

## Coordinated Disclosure

If you find a vulnerability that affects users on production HA
instances, please contact us privately first and allow at least 14 days
before public disclosure. We will work with you on the disclosure
timeline and credit you in the release notes if you would like.

## Hall of Fame

If you reported an issue that landed in a release, you'll be listed here
unless you ask otherwise.

_(none yet — be the first.)_
