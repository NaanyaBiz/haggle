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

If any pre-condition fails, report the failure and stop.

## Delegates to

The `release-manager` subagent, providing it:
- The target version string
- The current CHANGELOG.md `## [Unreleased]` section as context
- Instructions to update `manifest.json` + `CHANGELOG.md`, route the bump
  through a short-lived PR (the `protect-main` ruleset blocks direct
  commits to main), then create a **signed tag on the squash-merge commit**
  and push it

## After completion

Print the GitHub Release URL and confirm the seven attested assets exist (haggle.zip + provenance/SBOM sigstore bundles + SBOMs + check-runs.json)
(`haggle-<ver>.zip` + `.zip.sigstore`; verify with `gh attestation verify`).
