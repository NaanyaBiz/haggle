# Roadmap

This roadmap states what `haggle` intends to do — and deliberately will not do —
over roughly the next twelve months. It is a direction of travel, not a
contract: `haggle` is a single-maintainer, best-effort open-source project, and
priorities shift as AGL changes its API and as users report issues. Tracked work
lives in [GitHub issues](https://github.com/NaanyaBiz/haggle/issues); the
priority labels (`P1`/`P2`/`P3`) there are the live source of truth.

_Last reviewed: 2026-07. Reviewed at least annually and at each minor release._

## Direction (next ~12 months)

**Ship v0.4.0 stable — solar generation GA.** Complete the beta soak, verify
diagnostics end-to-end on a live install ([#159](https://github.com/NaanyaBiz/haggle/issues/159)),
and promote solar feed-in generation/credit statistics from beta to stable.

**Make Time-of-Use accurate and self-owned.**
- Derive tariff bands locally from user-configured ToU windows instead of
  trusting AGL's per-interval `consumption.type`
  ([#141](https://github.com/NaanyaBiz/haggle/issues/141)).
- Validate the plan-text ToU rate-mapping heuristic against a real ToU plan
  capture and correct it if AGL labels bands differently
  ([#90](https://github.com/NaanyaBiz/haggle/issues/90)).
- Fix per-band rate-sensor registration and bill projection on ToU contracts
  ([#126](https://github.com/NaanyaBiz/haggle/issues/126)).

**Improve the Energy-dashboard experience.** Resolve the cumulative-sensor
"decoy" in HA's source picker so users add the right statistics the first time
([#147](https://github.com/NaanyaBiz/haggle/issues/147)).

**Keep the engineering baseline healthy.** Decompose the one remaining
over-complexity function
([#187](https://github.com/NaanyaBiz/haggle/issues/187)), keep the OpenSSF
Scorecard posture verified ([#179](https://github.com/NaanyaBiz/haggle/issues/179)),
and maintain the secure-SDLC conformance record as control surfaces change.

## Non-goals (what `haggle` will *not* do)

These are deliberate scope boundaries, not backlog items:

- **One retailer only.** `haggle` targets AGL Australia's customer API. It will
  not grow into a multi-retailer abstraction.
- **Electricity (and its solar feed-in) only.** No gas, water, or other
  utilities.
- **Read-only.** `haggle` imports historical usage/cost/solar statistics into
  the HA Energy dashboard. It will not control devices, change tariffs, or take
  any write action on the AGL account.
- **No telemetry.** No phone-home, analytics, or external telemetry vendors —
  in the integration or in CI.
- **No portal scraping or OTP flows.** Authentication is Auth0 PKCE through the
  user's real browser; `haggle` will not automate the AGL web portal or handle
  credentials directly.
- **No destructive uninstall.** Removing the integration will not delete the
  user's accumulated `haggle:*` energy history
  ([#91](https://github.com/NaanyaBiz/haggle/issues/91), decided
  won't-implement).

## How priorities are set

Priority reflects consequence to users, not effort. Correctness of the
energy/cost data written to the recorder outranks new features; a defect that
writes wrong statistics or takes the integration down is always addressed before
enhancement work.
