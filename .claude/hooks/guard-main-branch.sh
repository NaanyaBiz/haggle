#!/usr/bin/env bash
# PreToolUse hook: refuse `git commit` / `git push` while on `main`.
# Rationale: this repo enforces a PR-only flow on main; direct commits
# bypass review and break the AI-generated provenance chain.
#
# Design (settled after five review passes on PR #269, each finding a
# different git invocation form the previous parser got wrong — the -C
# form entirely, "$PWD", globals before the subcommand, globals before
# -C, cumulative repeated -C, --git-dir/--work-tree, their space forms,
# quoted =-form values, and GIT_DIR= env assignments):
#
#   1. ONE tokenizer, not a matcher regex plus a separate extractor.
#      Those were two parsers that kept drifting apart; every "fix the
#      regex" round produced the next bypass.
#   2. GIT decides the branch. Target-selecting globals and env
#      assignments are replayed to a read-only `git rev-parse`, so git's
#      own option semantics (including cumulative -C) are authoritative
#      instead of re-implemented here.
#   3. The command string is NEVER executed. It is split on whitespace
#      with globbing disabled — no eval, no command substitution — and
#      only GIT_DIR / GIT_WORK_TREE / -C / --git-dir / --work-tree are
#      replayed. Every other option (-c, --exec-path, aliases) is
#      dropped, so nothing attacker-chosen reaches git.
#   4. Unresolvable targets DEFER (exit 0). This hook is the advisory
#      convenience layer; the enforced floor is the server-side
#      zero-bypass protect-main ruleset, which rejects the push. Accepted
#      residual: a variable that happens to expand to the main worktree
#      slips the LOCAL block; that commit is push-rejected and
#      recoverable with a reset.
set -euo pipefail

payload="$(cat)"
cmd="$(printf '%s' "$payload" | python3 -c 'import json, sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("command",""))' 2>/dev/null || true)"

if [[ -z "$cmd" ]]; then
    exit 0
fi

# Cheap pre-filter only — the tokenizer below decides for real.
if ! echo "$cmd" | grep -qE '(^|[[:space:]])(commit|push)([[:space:]]|$)'; then
    exit 0
fi

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

# `--opt="value"` / `GIT_DIR="value"`: the quotes sit around the VALUE,
# not the whole token, so boundary-stripping misses them (Codex pass-5).
unquote_assignment() {
    local tok="$1" name="${1%%=*}" value="${1#*=}"
    printf '%s=%s' "$name" "$(unquote "$value")"
    unset tok
}

set -f
read -ra _tokens <<< "$cmd"
set +f

env_args=()     # assignments bound to the git invocation being scanned
pending_env=()  # assignments seen since the last command boundary
targets=()      # -C / --git-dir / --work-tree and their operands
cd_dir=""       # `cd <path> && git commit` form
found=0         # a git commit/push invocation was identified
in_git=0
i=0
while [[ $i -lt ${#_tokens[@]} ]]; do
    raw="${_tokens[$i]}"
    tok="$(unquote "$raw")"
    if [[ $in_git -eq 0 ]]; then
        case "$tok" in
            git)
                in_git=1
                targets=()
                # A prefix assignment binds ONLY to the command it
                # precedes, so it becomes active exactly here.
                env_args=(${pending_env[@]+"${pending_env[@]}"})
                pending_env=()
                ;;
            cd)
                i=$((i + 1))
                cd_dir="$(unquote "${_tokens[$i]:-}")"
                pending_env=()
                ;;
            GIT_DIR=*|GIT_WORK_TREE=*)
                pending_env+=("$(unquote_assignment "$tok")")
                ;;
            *=*)
                : # some other assignment — irrelevant, and not a boundary
                ;;
            *)
                # Any other word starts a different command, so assignments
                # collected so far belonged to IT, not to a later git
                # (`GIT_DIR=x true && git -C main commit` — Codex pass-7).
                pending_env=()
                ;;
        esac
    else
        case "$tok" in
            commit|push)
                found=1
                break
                ;;
            -C|--git-dir|--work-tree)
                # Space form: the operand is the NEXT token (Codex pass-5).
                i=$((i + 1))
                targets+=("$tok" "$(unquote "${_tokens[$i]:-}")")
                ;;
            --git-dir=*|--work-tree=*)
                targets+=("$(unquote_assignment "$tok")")
                ;;
            -c|--namespace|--super-prefix|--config-env)
                # Arg-taking globals that do NOT retarget: skip the option
                # AND its operand, or the operand looks like a subcommand
                # and aborts the scan (`git -c commit.gpgsign=false commit`).
                i=$((i + 1))
                ;;
            -*)
                : # any other global option — deliberately not replayed
                ;;
            *)
                # A different subcommand (`git log`, `git status`): this
                # invocation is not ours. Keep scanning — a compound like
                # `git status && git commit` has another one later. Clear
                # BOTH target sources: the shell scopes `GIT_DIR=x git
                # status` to that invocation alone, so carrying the
                # assignment into the next one resolved the wrong repo
                # (Codex pass-6, PR #269).
                in_git=0
                targets=()
                env_args=()
                pending_env=()
                ;;
        esac
    fi
    i=$((i + 1))
done

if [[ $found -eq 0 ]]; then
    exit 0
fi

if [[ ${#targets[@]} -gt 0 || ${#env_args[@]} -gt 0 ]]; then
    branch="$(env ${env_args[@]+"${env_args[@]}"} git ${targets[@]+"${targets[@]}"} rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
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
