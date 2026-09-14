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

# Only inspect git commit / git push. Global options may sit between
# `git` and the subcommand (git -h: `git [-C <path>] [-c <name>=<value>]
# ... <command>`), so ONE pattern describes that run and BOTH the matcher
# and the -C extraction below use it — they were written separately and
# drifted twice: the original required the subcommand immediately after
# `git` (so every `-C`-form commit bypassed the guard and the extraction
# was dead code), and the pass-2 widening fixed only the matcher, leaving
# extraction blind to `git --no-pager -C <path> commit` (Codex passes 1-3,
# PR #269). Forms too exotic for a regex defer to the server-side
# zero-bypass ruleset, like every other unparseable command.
GIT_OPT='((-C|-c)[[:space:]]+[^[:space:]]+|--[^[:space:]]+|-[A-Za-z])'
GIT_CMD="(^|[[:space:]])git([[:space:]]+${GIT_OPT})*[[:space:]]+(commit|push)([[:space:]]|\$)"

if ! echo "$cmd" | grep -qE "$GIT_CMD"; then
    exit 0
fi

# Determine which repository the command actually targets, then ask GIT
# what branch that is — do NOT re-implement git's option semantics in
# regex. Five review passes each found another form the textual parser
# got wrong (the -C form entirely; "$PWD"; globals before the
# subcommand; globals before -C; then repeated -C, which git resolves
# CUMULATIVELY, and --git-dir/--work-tree, which retarget without -C at
# all — Codex passes 1-4, PR #269). Replaying the target-selecting
# globals to `git rev-parse` makes git the authority on its own CLI and
# closes that whole class.
#
# The command string is agent-supplied and is NEVER executed: it is
# split on whitespace with globbing disabled (no eval, no command
# substitution), and only -C / --git-dir / --work-tree — the three
# globals that change which repository is addressed — are replayed as
# argv to a read-only `rev-parse`. Everything else is ignored, so no
# attacker-chosen option (-c, --exec-path, aliases) reaches git.
invocation="$(echo "$cmd" | grep -oE "$GIT_CMD" | head -1)"

unquote() {
    local v="$1"
    v="${v%\"}"; v="${v#\"}"
    v="${v%\'}"; v="${v#\'}"
    # $PWD trivially expands to the caller's CWD; resolve it rather than
    # treating it as unresolvable (Codex pass-1, PR #269).
    # shellcheck disable=SC2016 # matching the LITERAL unexpanded string is the point
    if [[ "$v" == '$PWD' || "$v" == '${PWD}' ]]; then
        v="$PWD"
    fi
    printf '%s' "$v"
}

set -f
read -ra _tokens <<< "$invocation"
set +f

targets=()
i=0
while [[ $i -lt ${#_tokens[@]} ]]; do
    tok="$(unquote "${_tokens[$i]}")"
    case "$tok" in
        commit|push)
            break
            ;;
        -C|--git-dir|--work-tree)
            i=$((i + 1))
            targets+=("$tok" "$(unquote "${_tokens[$i]:-}")")
            ;;
        --git-dir=*|--work-tree=*)
            targets+=("$tok")
            ;;
    esac
    i=$((i + 1))
done

# `cd <path> && git commit` — the other way a command retargets.
cd_dir=""
if [[ ${#targets[@]} -eq 0 ]] && echo "$cmd" | grep -qE '^cd[[:space:]]+'; then
    cd_dir="$(unquote "$(echo "$cmd" | grep -oE '^cd[[:space:]]+[^[:space:]&;|]+' | awk '{print $2}')")"
fi

# An unresolvable target (unexpanded $VAR, nonexistent path) means we cannot
# know the branch. Defer to the server-side protect-main ruleset — the
# enforced zero-bypass floor; this hook is the advisory convenience layer —
# rather than guessing from the CWD, which is what false-blocked legitimate
# worktree commits. Accepted residual: a variable that happens to expand to
# the main worktree slips the LOCAL block; the commit is then rejected at
# push by the ruleset and recoverable with a reset.
if [[ ${#targets[@]} -gt 0 ]]; then
    branch="$(git "${targets[@]}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
elif [[ -n "$cd_dir" ]]; then
    [[ -d "$cd_dir" ]] || exit 0
    branch="$(git -C "$cd_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
else
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
fi
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
