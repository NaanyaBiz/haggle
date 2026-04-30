#!/usr/bin/env bash
# PreToolUse hook: refuse `git commit` / `git push` while sitting on `main`.
# Rationale: this repo enforces a PR-only flow on main; direct commits
# bypass review and break the AI-generated provenance chain.
set -euo pipefail

payload="$(cat)"
cmd="$(printf '%s' "$payload" | python3 -c 'import json, sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("command",""))' 2>/dev/null || true)"

if [[ -z "$cmd" ]]; then
    exit 0
fi

# Only inspect git commit / git push.
if ! echo "$cmd" | grep -qE '(^|[[:space:]])git[[:space:]]+(commit|push)([[:space:]]|$)'; then
    exit 0
fi

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
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
