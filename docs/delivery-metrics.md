# Delivery metrics (quarterly)

Run `python3 scripts/delivery_metrics.py` (needs an authenticated `gh`)
once a quarter, paste the two headline numbers into the log below, and fix
anything the reconciliation flags. That is the whole process — there is
deliberately no dashboard, no trend tooling, and no CI job (see Accepted
limitations).

## What is measured

- **Change-failure proxy** — share of releases followed within 7 days by a
  corrective release (one whose CHANGELOG section contains `### Fixed`).
  The stable-only number is the headline; beta→beta fix iterations are
  expected (that is what betas are for) and reported separately.
- **Bug-report → fix-release latency** — for each closed `bug` issue
  (excluding not-planned), days from the report to the first release
  published after it closed. Median reported.
- **Release-record reconciliation** — CHANGELOG version headings vs git
  tags vs GitHub releases must agree. A version that never shipped must
  say so on its heading line (`[NEVER RELEASED]`, `[YANKED]`, or
  "not yet formally released") — see `[0.3.2]` for the canonical example
  of why this check exists. Markers are cross-checked, not trusted: a
  never-released marker with an existing tag, or a yanked marker whose
  preserved tag is missing or whose GitHub release still exists, is
  flagged as a contradiction.

## Accepted limitations (recorded exception, CO-18.3)

**Time-to-restore is not measured, and will not be.** Distribution is
pull-based (HACS): there is no telemetry into users' Home Assistant
installs (deliberate — see `docs/diagnostics.md` for the privacy posture),
and "restore" happens per-household whenever each user chooses to upgrade,
typically within HACS's ~24 h tag-poll cadence of doing so. The measurable
compensating proxy is bug-report → fix-release latency above. This is a
deliberate, accepted limitation of the distribution model, not a to-do.
The same acceptance is recorded in the compliance program's risk register.

Per-tier / fleet trending is likewise declined: one asset, one tier —
there is nothing to trend across.

## Quarterly log

| Date | Releases | Change-failure (stable) | Change-failure (all) | Median bug→fix latency | Reconciliation |
|---|---|---|---|---|---|
| 2026-07-14 (baseline) | 16 | 1/6 | 8/15 | 1.5 d | `[0.3.2]` phantom found → annotated `[NEVER RELEASED]` |
