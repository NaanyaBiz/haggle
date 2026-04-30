#!/usr/bin/env bash
# UserPromptSubmit hook: prepend branch / worktree state to the user's
# prompt so the agent never confuses which branch it's editing.
# Output to stdout becomes a system message on the user's behalf.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [[ -z "$repo_root" ]]; then
    exit 0
fi

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "(unknown)")"
worktree="$(basename "$repo_root")"
dirty=""
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    dirty=" [dirty]"
fi

printf '<context-injection>repo=%s worktree=%s branch=%s%s</context-injection>\n' \
    "haggle" "$worktree" "$branch" "$dirty"
