# /pr

Push the current feature branch and open a GitHub pull request, after
documenting progress in CHANGELOG.md.

## Usage

```
/pr
/pr <optional PR title override>
```

## Pre-conditions (check before proceeding)

1. Working tree is clean (`git status --porcelain` returns empty).
   If dirty, stop and tell the user to commit or stash first.
2. Not on `main` — never open a PR from main.
   If on main, stop and report the error.

## Steps

### 1. Gather context

Run these in parallel:
- `git log main..HEAD --oneline` — list commits on this branch.
- `git diff main..HEAD --stat` — files changed.
- Read `CHANGELOG.md` to see the current `## [Unreleased]` section.

### 2. Update CHANGELOG.md

Add a new bullet (or bullets) under the appropriate sub-heading
(`### Added`, `### Changed`, `### Fixed`) in `## [Unreleased]` that
summarises what this branch contributes. Write in the same terse style
as existing entries. Do not duplicate anything already listed. Prefer
one concise sentence per logical capability added.

Commit the CHANGELOG update:
```
git add CHANGELOG.md
git commit -m "docs: update CHANGELOG for <branch-name>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

### 3. Push

```
git push -u origin <current-branch>
```

### 4. Open the PR

```
gh pr create \
  --base main \
  --title "<title>" \
  --body "<body>"
```

> **Note — merging from a worktree**: `gh pr merge` will fail because
> `main` is checked out in the primary worktree. Use the GitHub API
> instead:
> ```
> gh api repos/<owner>/<repo>/pulls/<number>/merge \
>   -X PUT -f merge_method=squash \
>   -f commit_title="<title> (#<number>)"
> gh api repos/<owner>/<repo>/git/refs/heads/<branch> -X DELETE
> ```
> After merging, run `/wt rm <branch>` to remove the local worktree.

**Title**: If the user supplied one, use it verbatim. Otherwise derive it
from the branch name and commits: conventional-commit prefix + short
summary (≤70 chars).

**Body** (use this template):

```
## Summary
<3-5 bullet points drawn from the commits and CHANGELOG entry>

## Test plan
- [ ] All existing tests pass (`uv run pytest`)
- [ ] mypy clean (`uv run mypy custom_components/haggle`)
- [ ] Pre-commit hooks pass
- <any manual steps the reviewer should run>

## CHANGELOG
<paste the new CHANGELOG lines verbatim>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

### 5. Report

Print the PR URL and remind the user that after the PR merges they can
clean up with `/wt rm <branch>`.
