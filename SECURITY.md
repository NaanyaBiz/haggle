# Security Policy

## Reporting a Vulnerability

Please report suspected security issues **privately** via either of:

1. **GitHub Private Security Advisories** —
   <https://github.com/NaanyaBiz/haggle/security/advisories/new>
   (preferred — keeps the reporter and the maintainer in a single thread).
2. **Email** — `security@naanya.biz`.

Please do **not** open a public issue for security reports.

We aim to acknowledge reports within **5 business days** and to publish a
fix or mitigation note in `CHANGELOG.md` within **30 days** of confirmation.
This is a single-maintainer hobby project; severe issues will be escalated,
but please calibrate expectations accordingly.

## Supported Versions

The latest tagged release on `main` is the only supported version. Beta
tags (`vX.Y.Z-beta.N`) receive fixes; older `v0.0.x-dev` tags do not. There
is no LTS branch.

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

## Threat Model Summary

The full threat model lives in `security/2026-05-02T04-43Z/stride/`. The
short version:

### Trust boundaries

| Boundary | Notes |
|---|---|
| AGL HTTPS API → HA coordinator | TLS + Trust-On-First-Use SPKI pinning (see below). |
| HA user browser → config flow | OAuth state nonce + PKCE S256. |
| AGL JSON → HA recorder/statistics | Allowlist-style parsing; numeric values clamped to non-negative finite floats. |
| GitHub Actions → HACS installers | All Actions SHA-pinned; release artefacts attested via `actions/attest-build-provenance`. |

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
  (`.storage/core.config_entries`). On Home Assistant OS this file is
  encrypted at rest; on Docker / venv / manual deployments it is plain
  JSON. We strongly recommend running the integration on HAOS or
  configuring an equivalent encrypted volume for `.storage/`.
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

**CI / workflows.**

- All GitHub Actions are pinned to 40-character commit SHAs with
  `# vX.Y` comments (Dependabot keeps them current, grouped weekly).
- Workflow-level `permissions: read-all` on `ci.yml`, `hacs.yml`,
  `hassfest.yml`, `scorecard.yml`. `release.yml` scopes
  `contents: write` + `id-token: write` + `attestations: write` to its
  single job.
- **No third-party actions in privileged workflows**: the GitHub Release
  is created with the first-party `gh` CLI, not a third-party action.
- **No external coverage vendor**: the Codecov upload was removed
  (write-only output, third-party code on every PR).
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
  `gh attestation`).

**Dev-machine hooks.** `.pre-commit-config.yaml` pins every remote hook
to a **frozen commit SHA** (refresh with `pre-commit autoupdate
--freeze`). `ruff` and `mypy` run via `uv run` (`language: system`) so
exactly one toolchain copy exists, versioned by `uv.lock` — the
tag-pinned mirror hooks previously drifted years behind the locked
versions. `gitleaks` stays as pre-commit secret scanning.

**Monitoring.** CodeQL (weekly + per-PR), OpenSSF Scorecard
(`scorecard.yml`, published results + README badge), Dependabot on both
ecosystems with grouped weekly PRs. At the 2026-07 review, direct
dependencies measured 5.4–7.4 on OpenSSF Scorecard (transitives up to
8.5); the weakest triaged link is `pyjwt` (exactly pinned by HA core,
sits in code paths this integration never imports — JWT expiry is
decoded with hand-rolled base64, not PyJWT).

**Own Scorecard posture.** First self-assessment (Scorecard v5.3.0,
2026-07-12): **7.0/10**, with 10s on Pinned-Dependencies, SAST, CI-Tests,
Dangerous-Workflow, Dependency-Update-Tool, Security-Policy, License,
Binary-Artifacts and Vulnerabilities. Remaining deductions, triaged:

- `Maintained: 0` — pure repo-age gate (<90 days); self-resolves ~2026-08.
- `Code-Review: 0`, `Contributors: 0` — measure team structure (a second
  human reviewer; multi-organization contributors). **Accepted at 0** for
  a solo-maintained repo; bot reviews (Codex/Claude) are excluded by
  Scorecard's design and self-review would be theater.
- `Packaging: -1` — means "no PyPI/registry publish workflow"; not
  applicable to a HACS-distributed integration. Inconclusive checks are
  excluded from the aggregate. Accepted.
- `Branch-Protection: -1` — default workflow token cannot read classic
  protection rules; remediation is a ruleset on `main` (#171), not a PAT
  secret in CI.
- `CII-Best-Practices: 0` — bestpractices.dev registration tracked in
  #172 (passing level only; silver+ requires two-person review).
- `Fuzzing: 0` — **implemented 2026-07-12** (#173): atheris harness
  (`tests/fuzz/fuzz_parser.py`) enforcing parser totality and the
  finite/non-negative numeric guarantee, weekly + per-parser-change CI
  (`fuzz.yml`, hash-pinned install). If the next weekly Scorecard run
  does not credit the atheris integration, ClusterFuzzLite is the
  recognized fallback.
- `Signed-Releases: -1` — releases carried no assets; from the next tag,
  `release.yml` uploads `haggle-<ver>.zip` plus its Sigstore bundle
  (`.zip.sigstore`), verifiable offline or via
  `gh attestation verify haggle-<ver>.zip --repo NaanyaBiz/haggle`.

Realistic ceiling ≈ 8.5–9: the residual gap is the team-structure checks
above, which a single-maintainer project cannot honestly score on.

**Accepted risks (diminishing-returns line).**
`pytest-homeassistant-custom-component` is a single-maintainer package
tracked as a range (`<0.14`) — vendoring it would mean self-maintaining
a fork of HA's test harness forever. The HA transitive tree is accepted
as-is. Detection tooling (CodeQL, gitleaks, Scorecard) is kept even
though each adds a component: it is detection surface, not attack
surface.

## Coordinated Disclosure

If you find a vulnerability that affects users on production HA
instances, please contact us privately first and allow at least 14 days
before public disclosure. We will work with you on the disclosure
timeline and credit you in the release notes if you would like.

## Hall of Fame

If you reported an issue that landed in a release, you'll be listed here
unless you ask otherwise.

_(none yet — be the first.)_
