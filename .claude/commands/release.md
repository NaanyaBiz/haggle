# /release

Cut a new semantic-versioned release.

## Usage

```
/release <version>
```

Example:
```
/release 0.1.0
```

## Pre-conditions (checked before proceeding)

1. Working tree is clean (`git status --porcelain` returns empty).
2. On `main` branch.
3. Latest CI run on `main` is green (`gh run list --branch main --limit 1`).
4. **Stable versions only** (no `-` in the version): the acceptance gate
   of `docs/releasing.md` passes —
   a. Beta soak ≥ 7 days since the newest `v<version>-beta.*` release
      (compute below), OR Dave supplies an explicit hotfix-validation
      statement to embed in the release PR;
   b. `gh issue list --label beta-blocker --state open` is empty;
   c. Dave confirms the app-reconciliation result and downgrade-test
      result to record (ask; do not invent numbers).

   ```bash
   LAST_BETA_DATE=$(gh release list --limit 40 --json tagName,publishedAt \
     --jq "[.[] | select(.tagName | startswith(\"v$VERSION-\"))] | first | .publishedAt // empty")
   if [ -n "$LAST_BETA_DATE" ]; then
     python3 -c "from datetime import datetime,timezone;d=datetime.fromisoformat('$LAST_BETA_DATE'.replace('Z','+00:00'));print('soak days:',(datetime.now(timezone.utc)-d).days)"
   fi
   ```

If any pre-condition fails, report the failure and stop.

## Delegates to

The `release-manager` subagent, providing it:
- The target version string
- The current CHANGELOG.md `## [Unreleased]` section as context
- Instructions to update `manifest.json` + `CHANGELOG.md` (including the
  escaped-defect count line — release-manager "Files to update" step 4),
  route the bump
  through a short-lived PR (the `protect-main` ruleset blocks direct
  commits to main), then create a **signed tag on the squash-merge commit**
  and push it
- For stable releases, the acceptance evidence gathered above, to be
  embedded verbatim in the release PR body

## After completion

Print the GitHub Release URL and confirm the seven attested assets exist (haggle.zip + provenance/SBOM sigstore bundles + SBOMs + check-runs.json)
(`haggle-<ver>.zip` + `.zip.sigstore`; verify with `gh attestation verify`).
