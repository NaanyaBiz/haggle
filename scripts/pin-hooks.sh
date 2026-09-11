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

shopt -s nullglob
scripts=(.claude/hooks/*.sh)
shopt -u nullglob
if [[ ${#scripts[@]} -eq 0 ]]; then
    echo "Error: no hook scripts found under .claude/hooks/" >&2
    exit 1
fi

# Show the reviewer exactly what trust is being granted: per-script git
# status (a Modified/Untracked marker means you are pinning content that
# is not what HEAD ships) and the hash delta vs the existing pins.
echo "Pinning ${#scripts[@]} hook script(s) on branch: $(git rev-parse --abbrev-ref HEAD)"
for s in "${scripts[@]}"; do
    status="$(git status --porcelain -- "$s")"
    new_hash="$(shasum -a 256 "$s" | cut -d' ' -f1)"
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

shasum -a 256 "${scripts[@]}" > "$PIN_FILE"
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
    echo "Error: jq is required to install the hook wiring (brew install jq)" >&2
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

if [[ -f "$LOCAL_SETTINGS" ]]; then
    current="$(jq -S '.hooks // {}' "$LOCAL_SETTINGS")"
else
    current="{}"
    echo "{}" > "$LOCAL_SETTINGS"
fi
proposed="$(jq -S '.hooks' "$WIRING_TEMPLATE")"

if [[ "$current" == "$proposed" ]]; then
    echo "Hook wiring in $LOCAL_SETTINGS already matches the template."
else
    echo "Updating hook wiring in $LOCAL_SETTINGS (diff vs installed):"
    diff <(printf '%s\n' "$current") <(printf '%s\n' "$proposed") || true
    tmp="$(mktemp)"
    jq --slurpfile w "$WIRING_TEMPLATE" '.hooks = $w[0].hooks' "$LOCAL_SETTINGS" > "$tmp"
    mv "$tmp" "$LOCAL_SETTINGS"
    echo "Wiring installed. Claude Code picks it up via its settings watcher."
fi
