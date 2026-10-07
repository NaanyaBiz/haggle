#!/usr/bin/env bash
# Pre-commit `commit-msg` hook: every commit declares its provenance.
#
# Most of this repo is AI-generated and human-reviewed (AGENTS.md
# § Provenance). Each commit must say which AI tool, if any, produced or
# co-authored it, so the record is honest whatever the vendor (#283):
#
#   Co-Authored-By: <AI tool> <email>   one line per tool (Claude, Codex,
#                                       DeepSeek, Copilot, ...)
#   AI-Assisted: none                   written without AI assistance
#
# Humans co-author by adding their own Co-Authored-By trailer as well.
# Merge commits are skipped (git generates their messages).
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

# A named co-author (any AI tool, any vendor) ...
if printf '%s' "$msg" | grep -qiE '^Co-Authored-By:[[:space:]]*[^[:space:]]'; then
    exit 0
fi
# ... or an explicit declaration that no AI was involved.
if printf '%s' "$msg" | grep -qiE '^AI-Assisted:[[:space:]]*none[[:space:]]*$'; then
    exit 0
fi

cat >&2 <<'MSG'

  Commit rejected: no provenance trailer.

  Every commit says which AI tool, if any, produced or co-authored it.
  Append ONE of these as the last paragraph of your commit message:

      Co-Authored-By: <AI tool> <email>   e.g. Co-Authored-By: Claude <noreply@anthropic.com>
      AI-Assisted: none                   the change was written without AI assistance

  Name the tool you actually used (any vendor). A trailer that is not
  true is worse than none. See CONTRIBUTING.md > Commit conventions.

MSG
exit 1
