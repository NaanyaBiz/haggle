# Repo settings as code

This directory is the **declared state** of the GitHub control plane for this
repository — the one part of the delivery environment that is not already
version-controlled code (secure-SDLC standard CO-9 — docs/compliance/secure-sdlc-standard.md). A personal-account repo
has no audit log, so these baselines + the weekly drift check are the only
reviewable record of settings changes.

| File | Contents | Drift-checked |
|---|---|---|
| `ruleset-<name>.json` | One per live ruleset, normalized (`scripts/normalize-ruleset.jq`) | Weekly, by `settings-drift.yml` |
| `repo-public.json` | Anonymous-visible repo settings (`scripts/normalize-repo-public.jq`) | Weekly, by `settings-drift.yml` |
| `repo-admin-snapshot.json` | Admin-only settings: merge methods, security & analysis toggles, Actions policy + selected-actions allowlist, ruleset bypass actors, classic branch protection (recorded as `null` since its deliberate retirement, 2026-07-13), secrets/hooks/keys inventory | **No** — reading these needs `administration` scope, which the workflow `GITHUB_TOKEN` cannot hold, and adding a standing PAT is against the zero-standing-secrets posture (#171). Refreshed manually. |

## Process rule — settings changes go PR-first

1. Open a PR that updates the relevant file(s) here to the **intended** state
   (describe the why in the PR body).
2. Merge it (green checks as usual).
3. Apply the change in GitHub (UI or `gh api`), then run
   `./scripts/export-settings.sh` and confirm `git status` is clean — the
   live state now matches the declared state. If the export differs from the
   merged baseline, reconcile immediately (fix the setting or PR a correction).

Break-glass (urgent settings change applied live first): allowed, but the
reconciliation PR (`export-settings.sh` output) must follow within a week —
before the next scheduled drift run — or the drift issue will file itself.
The documented break-glass for the `v*` tag ruleset (bad release tag: disable
→ fix → re-enable) is drift-invisible if completed within one run window, but
record it in an issue regardless — control-plane changes are otherwise
invisible on a personal account.

## When the weekly check flags drift

`settings-drift.yml` opens/updates an issue labelled `settings-drift` and the
run goes red. Reconcile **deliberately**: either revert the live setting, or
adopt it by PRing the refreshed export. Never silently re-export to make the
diff go away — the diff is the change record.

## Known blind spots (accepted)

- `bypass_actors` on rulesets is only returned to callers with write access
  to the ruleset, so the weekly job cannot see it; it is recorded in
  `repo-admin-snapshot.json` and re-verified on every manual export.
- Everything in `repo-admin-snapshot.json` drifts silently between manual
  exports. Compensating controls: the process rule above, and the weekly
  OpenSSF Scorecard re-measurement (Branch-Protection, Token-Permissions).
- The repo owner holds admin and can edit any of this at any time — detection
  is possible, prevention is not (single-maintainer reality, see SECURITY.md).
