#!/usr/bin/env bash
# Pre-commit `commit-msg` hook: enforce Co-Authored-By: Claude trailer.
#
# This repo is purely AI-generated; provenance is mandatory. Every commit must
# carry a Co-Authored-By trailer naming Claude. Humans co-author by adding
# their own trailer in addition.
#
# Skip merge commits (they auto-generate messages).
set -euo pipefail

msg_file="${1:?missing commit-msg file path}"
msg="$(cat "$msg_file")"

# Skip merges
if [[ -n "${GIT_REFLOG_ACTION:-}" && "${GIT_REFLOG_ACTION}" == merge* ]]; then
    exit 0
fi
if printf '%s' "$msg" | head -1 | grep -qE '^Merge '; then
    exit 0
fi

if printf '%s' "$msg" | grep -qiE '^Co-Authored-By:.*Claude'; then
    exit 0
fi

cat >&2 <<'EOF'

  Commit rejected: missing `Co-Authored-By: Claude` trailer.

  This repo is purely AI-generated; every commit must carry a Claude
  co-author trailer for provenance. Append to your commit message:

      Co-Authored-By: Claude <noreply@anthropic.com>

  See AGENTS.md > Commit conventions.

EOF
exit 1
