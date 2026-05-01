# /pr

Push the current feature branch and open a GitHub pull request, after
running a full documentation audit.

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
3. Branch is rebased onto current `main`.
   Run `git fetch origin`, then check `git rev-list HEAD..origin/main --count`.
   If the count is > 0 (main has commits this branch doesn't have):
   - Run `git rebase origin/main`.
   - If it succeeds (exit 0), continue — but note that history was rewritten,
     so a `git push --force-with-lease origin <branch>` will be needed in Step 3.
   - If it has conflicts, stop immediately: list the conflicting files from
     `git diff --name-only --diff-filter=U`, tell the user to resolve them
     manually and re-run `/pr` when done.
   If count is 0 (already up to date), continue with a regular push in Step 3.

## Steps

### 1. Gather context

Run these in parallel:
- `git log main..HEAD --oneline` — list commits on this branch.
- `git diff main..HEAD --stat` — files changed.
- Read `CHANGELOG.md` (current `## [Unreleased]` section).
- Read `AGENTS.md` (Repo Map + AGL API sections).

### 2. Documentation audit (REQUIRED — do not skip)

Work through the full checklist from `AGENTS.md § Documentation Checklist`.
All four artifact types must be addressed before the PR is opened.

**CHANGELOG.md** — add bullet(s) under the appropriate sub-heading
(`### Added`, `### Changed`, `### Fixed`) in `## [Unreleased]`.
One concise sentence per logical capability. Do not duplicate existing entries.

**AGENTS.md — Repo Map** — add any new files introduced by this branch;
update descriptions if a file's role changed. Cross-check by running:
```
find custom_components/haggle tests -name "*.py" | sort
```

**AGENTS.md — AGL API** — if this branch corrected an API fact (endpoint,
field name, token lifetime, header), update that bullet now.

**AGENTS.md — What NOT to Do** — if a new footgun was discovered, add it.

**Memory files** — record any non-obvious decision, confirmed API behaviour,
or user preference that should survive context resets. Write to
`~/.claude/projects/<project>/memory/`.

**Sprint/phase boundary** — if this branch completes a named sprint or phase:
- Move completed items out of `## [Unreleased]` into a dated `## [x.y.z-dev]` entry.
- Update `## [Unreleased]` → `### Targets for next sprint`.
- Do a full Repo Map audit against the actual file tree.
- Review every AGL API bullet against the current implementation.

Commit all documentation changes together:
```
git add AGENTS.md CHANGELOG.md
git commit -m "docs: update AGENTS.md and CHANGELOG for <branch-name>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

### 3. Push

If the rebase in pre-condition 3 rewrote history, use:
```
git push --force-with-lease -u origin <current-branch>
```
Otherwise use a regular push:
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

## Documentation updated
- [ ] CHANGELOG.md — new entries under [Unreleased]
- [ ] AGENTS.md — Repo Map reflects current file tree
- [ ] AGENTS.md — AGL API facts verified / corrected
- [ ] Memory files updated (or N/A — nothing non-obvious to record)

## Test plan
- [ ] All existing tests pass (`uv run pytest`)
- [ ] mypy clean (`uv run mypy custom_components/haggle`)
- [ ] Pre-commit hooks pass
- <any manual steps the reviewer should run>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

### 5. Report

Print the PR URL and remind the user that after the PR merges they can
clean up with `/wt rm <branch>`.
