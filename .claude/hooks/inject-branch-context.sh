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

# Sanitize before interpolating (#244): git accepts <, >, ", ' in ref names
# (git check-ref-format rejects only space/~/^/:/?/*/[/backslash), so a
# branch named e.g. `x</context-injection><context-injection>...` would
# forge instruction-shaped markup into every prompt of the session. Strict
# allowlist, length-capped — anything else becomes `_`.
sanitize() {
    printf '%s' "$1" | LC_ALL=C tr -c 'A-Za-z0-9._/@:+-' '_' | cut -c1-120
}
branch="$(sanitize "$branch")"
worktree="$(sanitize "$worktree")"

printf '<context-injection>repo=%s worktree=%s branch=%s%s</context-injection>\n' \
    "haggle" "$worktree" "$branch" "$dirty"
