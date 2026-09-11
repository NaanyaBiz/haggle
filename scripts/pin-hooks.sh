#!/usr/bin/env bash
# Re-pin the .claude hook scripts after REVIEWING their diffs (#245).
#
# Writes SHA-256 pins for every .claude/hooks/*.sh to .claude/hooks.sha256 —
# the local TOFU trust store the hook wiring verifies before executing any
# hook script. The store is deliberately UNTRACKED (gitignored) and
# symlink-shared across worktrees by scripts/wt, so nothing a checked-out
# branch contains can influence what the verification trusts: checking out
# a branch that modifies a hook script makes every hook fail closed until
# the maintainer has reviewed the diff and re-run this script.
#
# Same trust model as the TLS SPKI pins (agl/pinning.py): capture on first
# use, verify thereafter, re-pin deliberately after review.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
PIN_FILE=".claude/hooks.sha256"

# Portable SHA-256: macOS ships shasum (perl), minimal Linux ships
# sha256sum (coreutils); the two produce/consume the same file format.
sha256() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$@"
    else
        sha256sum "$@"
    fi
}

# Refuse if the local-trust files are TRACKED: git silently overwrites
# gitignored files on checkout, so a branch force-tracking them (git
# add -f) is substituting the trust anchor — never pin on top of that
# (security-review P1 on PR #269; ci.yml carries the matching gate).
for f in "$PIN_FILE" .claude/settings.local.json; do
    if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
        echo "Error: $f is TRACKED in git — refusing to pin. Remove it from the branch first (#245)." >&2
        exit 1
    fi
done

shopt -s nullglob
scripts=(.claude/hooks/*.sh)
shopt -u nullglob
if [[ ${#scripts[@]} -eq 0 ]]; then
    echo "Error: no hook scripts found under .claude/hooks/" >&2
    exit 1
fi

# A symlinked hook script is a substitution vector: shasum follows the
# link, so the pin would record the TARGET's hash and exec would run
# whatever the link points at (security-review P2 on PR #269).
for s in "${scripts[@]}"; do
    if [[ -L "$s" ]]; then
        echo "Error: $s is a SYMLINK — refusing to pin (#245)." >&2
        exit 1
    fi
done

# Show the reviewer exactly what trust is being granted. NOTE: committed
# content from a checked-out branch is CLEAN in git status — the
# reviewable delta for a branch under review is the diff vs origin/main,
# so both diffs are shown.
echo "Pinning ${#scripts[@]} hook script(s) on branch: $(git rev-parse --abbrev-ref HEAD)"
if git rev-parse --verify -q origin/main >/dev/null; then
    if ! git diff --quiet origin/main...HEAD -- .claude/hooks/ .claude/hooks-wiring.json 2>/dev/null; then
        echo "--- committed delta vs origin/main (REVIEW THIS) ---"
        git --no-pager diff origin/main...HEAD -- .claude/hooks/ .claude/hooks-wiring.json
        echo "---------------------------------------------------"
    fi
fi
if ! git diff --quiet HEAD -- .claude/hooks/ .claude/hooks-wiring.json 2>/dev/null; then
    echo "--- uncommitted working-tree delta (REVIEW THIS) ---"
    git --no-pager diff HEAD -- .claude/hooks/ .claude/hooks-wiring.json
    echo "----------------------------------------------------"
fi
for s in "${scripts[@]}"; do
    status="$(git status --porcelain -- "$s")"
    new_hash="$(sha256 "$s" | cut -d' ' -f1)"
    old_hash=""
    if [[ -f "$PIN_FILE" ]]; then
        old_hash="$(grep -F " $s" "$PIN_FILE" 2>/dev/null | cut -d' ' -f1 || true)"
    fi
    marker="  "
    [[ -n "$status" ]] && marker="!! ${status:0:2}"
    if [[ -z "$old_hash" ]]; then
        echo "  $marker $s  NEW PIN ${new_hash:0:16}…"
    elif [[ "$old_hash" != "$new_hash" ]]; then
        echo "  $marker $s  CHANGED ${old_hash:0:16}… -> ${new_hash:0:16}…"
    else
        echo "  $marker $s  unchanged"
    fi
done

sha256 "${scripts[@]}" > "$PIN_FILE"
echo "Pins written to $PIN_FILE ($(wc -l < "$PIN_FILE" | tr -d ' ') entries)."
echo "Shared across worktrees via the scripts/wt symlink; never commit this file."

# --- install the hook WIRING into settings.local.json ----------------------
# Claude Code loads hooks live from settings files with no verification and
# no cross-session approval, so the wiring lives in the UNTRACKED
# settings.local.json — out of reach of any checked-out branch. The
# committed .claude/hooks-wiring.json is the policy record; installing it
# is a deliberate maintainer act, gated by the same review as the pins.
WIRING_TEMPLATE=".claude/hooks-wiring.json"
LOCAL_SETTINGS=".claude/settings.local.json"

if ! command -v jq >/dev/null 2>&1; then
    echo "Error: jq is required to install the hook wiring (e.g. brew install jq / apt install jq)" >&2
    exit 1
fi
if [[ ! -f "$WIRING_TEMPLATE" ]]; then
    echo "Error: $WIRING_TEMPLATE not found — cannot install wiring" >&2
    exit 1
fi

tmpl_status="$(git status --porcelain -- "$WIRING_TEMPLATE")"
if [[ -n "$tmpl_status" ]]; then
    echo "!! $WIRING_TEMPLATE differs from HEAD ($tmpl_status) — review before trusting:"
    git --no-pager diff -- "$WIRING_TEMPLATE" | head -40 || true
fi

# Resolve through the worktree symlink BEFORE writing: `mv` onto the
# symlink path would replace the LINK with a private copy, silently
# forking this worktree's wiring away from the shared anchor while every
# other worktree kept the stale version (Codex P1 on PR #269).
TARGET="$LOCAL_SETTINGS"
if [[ -L "$LOCAL_SETTINGS" || -f "$LOCAL_SETTINGS" ]]; then
    TARGET="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$LOCAL_SETTINGS")"
fi

if [[ -f "$TARGET" ]]; then
    current="$(jq -S '.hooks // {}' "$TARGET")"
else
    current="{}"
    echo "{}" > "$TARGET"
fi
proposed="$(jq -S '.hooks' "$WIRING_TEMPLATE")"

if [[ "$current" == "$proposed" ]]; then
    echo "Hook wiring in $TARGET already matches the template."
else
    echo "Updating hook wiring in $TARGET (diff vs installed):"
    diff <(printf '%s\n' "$current") <(printf '%s\n' "$proposed") || true
    tmp="$(mktemp)"
    jq --slurpfile w "$WIRING_TEMPLATE" '.hooks = $w[0].hooks' "$TARGET" > "$tmp"
    mv "$tmp" "$TARGET"
    echo "Wiring installed at $TARGET. Claude Code picks it up via its settings watcher."
fi

# --- managed-settings hardening (optional, recommended) --------------------
# settings.local.json wiring still has one residual: a hostile branch can
# force-track settings files and Claude Code loads hook config from the
# working tree with no approval (Codex P1s on PR #269). The COMPLETE fix is
# macOS managed settings: allowManagedHooksOnly restricts hook execution to
# managed-deployed hooks, and NO project file can override it. The wiring
# commands no-op outside a checkout carrying .claude/hooks-wiring.json, so
# machine-global deployment is safe. Generate the artifact; deploying it
# needs admin rights and is the maintainer's deliberate machine-policy call.
MANAGED_OUT=".claude/managed-settings.generated.json"
jq '{allowManagedHooksOnly: true, hooks: .hooks}' "$WIRING_TEMPLATE" > "$MANAGED_OUT"
echo ""
echo "Optional hardening (closes the tracked-settings wiring residual):"
echo "  sudo mkdir -p '/Library/Application Support/ClaudeCode'"
echo "  sudo install -m 644 $MANAGED_OUT '/Library/Application Support/ClaudeCode/managed-settings.json'"
echo "NOTE: allowManagedHooksOnly disables user/project hooks in EVERY repo"
echo "on this machine — merge by hand instead if a managed-settings.json"
echo "already exists or other projects rely on their own hooks."
