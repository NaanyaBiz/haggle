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
- Instructions to update `manifest.json`, `CHANGELOG.md`, commit, tag, and push

## After completion

Print the GitHub Release URL and remind about flipping the repo to public when submitting to HACS.
