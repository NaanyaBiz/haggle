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

- All GitHub Actions are pinned to 40-character commit SHAs with
  `# vX.Y` comments (Dependabot keeps them current).
- Workflow-level `permissions: read-all` on `ci.yml`, `hacs.yml`,
  `hassfest.yml`. `release.yml` declares only `contents: write`.
- The HACS validation step runs without `continue-on-error`.
- Release tags trigger an attested GitHub Release via
  `actions/attest-build-provenance`.

## Coordinated Disclosure

If you find a vulnerability that affects users on production HA
instances, please contact us privately first and allow at least 14 days
before public disclosure. We will work with you on the disclosure
timeline and credit you in the release notes if you would like.

## Hall of Fame

If you reported an issue that landed in a release, you'll be listed here
unless you ask otherwise.

_(none yet — be the first.)_
