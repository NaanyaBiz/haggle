# Release acceptance policy

Channel model: every MINOR ships as `vX.Y.0-beta.N` prereleases on the
HACS beta channel first, then promotes to stable with zero code diff.
PATCH releases (hotfixes) may skip the ladder only under the hotfix rule
below. `release.yml` marks any tag containing `-` as a prerelease.

## Beta-soak rule (standing)

A stable `vX.Y.0` may be tagged only when ALL of:

1. **Soak**: the latest `vX.Y.0-beta.N` has run ≥ 7 days on a volunteer
   beta tester's live HA instance, reported on the relevant GitHub issue,
   with zero regressions attributable to it. The maintainer no longer
   holds a live AGL account (RA-16, SECURITY.md) and cannot dogfood this
   himself.
2. **Reconciliation**: a volunteer beta tester's Energy dashboard daily
   kWh (and, on solar contracts, sold-to-grid kWh) reconciles with the
   AGL app for at least one complete recent day (delta ≤ 0.05 kWh — AGL
   rounds to 2 dp), reported on the release-tracking issue.
3. **Zero beta blockers**: `gh issue list --label beta-blocker --state
   open` is empty. Tag beta-user reports that must gate promotion with
   `beta-blocker` as they arrive.

## Hotfix rule (stables that skip the ladder)

A PATCH stable fixing a severe defect may ship without beta soak ONLY
with recorded validation evidence in the release PR: what was verified,
against what ground truth (live recorder inspection, AGL portal CSV,
app reconciliation), on which HA version. "CI is green" alone is not
validation evidence for a statistics-path fix.

## Acceptance evidence record

The `chore(release)` PR body for every stable release carries an
"Acceptance evidence" section (the release-manager agent enforces this):
soak duration or hotfix evidence, reconciliation result, beta-blocker
count, and the downgrade-test result (below). The CHANGELOG promote
entry summarises the same in one line. This PR body is the release's
acceptance sign-off record.

## Downgrade test (once per stable line)

Before or with each stable `vX.Y.z`, run one manual downgrade test from
the new version to the previous stable and record the result in the
release PR + CHANGELOG. This requires a live AGL-authenticated poll
(step 3 below), which the maintainer can no longer perform himself
(RA-16) — it is now done by or with a volunteer beta tester's install.
If no volunteer is available for a given stable line, record that gap
explicitly in the release PR rather than skip the test silently:

1. On an HA instance running the new version: note the last poll time
   and current Energy-dashboard daily totals.
2. HACS → Haggle → ⋮ → Redownload → select the previous stable →
   restart HA.
3. Verify: config entry loads (no repair/setup error), a manual poll
   or the next scheduled poll succeeds, daily kWh totals unchanged
   (no double-count — imports are idempotent on (statistic_id, start)).
4. Redownload back to the new version → restart → verify the entry
   loads and (0.4.x+) any solar heal record resumes without a re-sweep
   storm (one bounded backfill of the gap is expected and correct).

## Risk acceptances (structural, not fixable by process)

- **No second human acceptor.** Acceptance is the maintainer accepting
  volunteer beta testers' reports on issues — since 2026-07-28 the
  maintainer no longer dogfoods on his own AGL account at all (RA-16),
  so acceptance now depends entirely on external volunteers rather than
  just lacking a second reviewer. Accepted; the compensating control is
  the recorded evidence above, held pending a volunteer if none confirm.
- **No automated health-based rollback.** Pull-based, telemetry-free
  distribution cannot observe installed-base health or push a rollback.
  Building telemetry to satisfy this clause would be worse than the gap.
  Remediation on failure = yank the release + fast forward-fix
  (demonstrated: v0.1.0 yank; v0.2.1-3 within 28 h).
