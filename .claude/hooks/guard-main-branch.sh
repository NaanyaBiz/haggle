#!/usr/bin/env bash
# PreToolUse hook: refuse `git commit` / `git push` while on `main`.
# Rationale: this repo enforces a PR-only flow on main; direct commits
# bypass review and break the AI-generated provenance chain.
set -euo pipefail

payload="$(cat)"
cmd="$(printf '%s' "$payload" | python3 -c 'import json, sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("command",""))' 2>/dev/null || true)"

if [[ -z "$cmd" ]]; then
    exit 0
fi

# Only inspect git commit / git push — including the `git -C <path>` form,
# which the old pattern (commit/push required IMMEDIATELY after `git`)
# never matched at all: every -C-form commit bypassed the guard and the
# -C extraction below was dead code (found chasing Codex P2 on PR #269).
if ! echo "$cmd" | grep -qE '(^|[[:space:]])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+(commit|push)([[:space:]]|$)'; then
    exit 0
fi

# Determine the git directory targeted by this command.
# Handle: `git -C /some/path commit` and `cd /some/path && git commit`.
git_dir="."
if echo "$cmd" | grep -qE 'git[[:space:]]+-C[[:space:]]+'; then
    git_dir="$(echo "$cmd" | grep -oE 'git[[:space:]]+-C[[:space:]]+[^[:space:]]+' | head -1 | awk '{print $NF}')"
elif echo "$cmd" | grep -qE '^cd[[:space:]]+'; then
    git_dir="$(echo "$cmd" | grep -oE '^cd[[:space:]]+[^[:space:]&;|]+' | awk '{print $2}')"
fi
# Strip surrounding quotes: `cd "$WT" && git commit` captured `"$WT"` with
# literal quotes; the unresolvable string then fell back to the CWD (often
# the main worktree) and false-blocked legitimate worktree commits
# (hook-robustness cluster with #244/#245, observed 2026-09-09).
git_dir="${git_dir%\"}"; git_dir="${git_dir#\"}"
git_dir="${git_dir%\'}"; git_dir="${git_dir#\'}"

# $PWD trivially expands to the CWD — resolve it instead of treating it
# as unresolvable, so `git -C "$PWD" commit` on main still blocks
# (Codex P2 on PR #269).
# shellcheck disable=SC2016 # matching the LITERAL unexpanded string is the point
if [[ "$git_dir" == '$PWD' || "$git_dir" == '${PWD}' ]]; then
    git_dir="."
fi

# Any OTHER unresolvable target (unexpanded $VAR, nonexistent path) means we
# cannot know the branch. Defer to the server-side protect-main ruleset —
# the enforced zero-bypass floor; this hook is the advisory convenience
# layer — rather than guessing from the CWD, which is what false-blocked
# legitimate worktree commits. Accepted residual: a variable that happens
# to expand to the main worktree slips the LOCAL block; the commit is then
# rejected at push by the ruleset and recoverable with a reset.
if [[ "$git_dir" != "." && ! -d "$git_dir" ]]; then
    exit 0
fi

branch="$(git -C "$git_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
if [[ "$branch" == "main" ]]; then
    # Allow bypass via env var in the *calling shell* OR as a prefix in the
    # command string (e.g. `HAGGLE_ALLOW_MAIN_PUSH=1 git commit ...`).
    if [[ "${HAGGLE_ALLOW_MAIN_PUSH:-}" == "1" ]]; then
        exit 0
    fi
    if echo "$cmd" | grep -qE '(^|[[:space:]])HAGGLE_ALLOW_MAIN_PUSH=1([[:space:]]|$)'; then
        exit 0
    fi

    cat >&2 <<EOF

  Blocked: refusing git commit/push on branch 'main'.

  This repo uses a PR-only flow. Create a feature branch via:

      ./scripts/wt new <branch>

  ...then commit and push there, and open a PR with 'gh pr create'.

  Override (scaffold only): prefix your command with HAGGLE_ALLOW_MAIN_PUSH=1

EOF
    exit 2
fi
exit 0
