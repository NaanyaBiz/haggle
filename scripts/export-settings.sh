#!/usr/bin/env bash
# Re-export the GitHub control-plane state into .github/settings/.
#
# Run with an ADMIN-scoped `gh` login (the maintainer's own auth) whenever a
# repo setting or ruleset changes — PR-first: commit the refreshed baselines
# in the same PR that records the settings change (.github/settings/README.md).
#
# The settings-drift workflow re-runs the ruleset + public-settings exports
# with the unprivileged per-run GITHUB_TOKEN and diffs byte-for-byte, so this
# script and the workflow MUST share the same jq normalizers (scripts/*.jq).
#
# Failure discipline (Codex review, #188): every API read either succeeds or
# aborts the export — a transient error must never masquerade as declared
# state (deleted baselines, null-ed policies, or partial arrays).
set -euo pipefail

REPO=${1:-NaanyaBiz/haggle}
cd "$(git rev-parse --show-toplevel)"
OUT=.github/settings
mkdir -p "$OUT"

# --- 1. Rulesets: one normalized file per live ruleset (CI drift-checked) ---
# Fetch the id list BEFORE deleting anything: under `set -e` this assignment
# aborts on API failure, whereas a $(...) inside the for-list would not —
# and an empty loop after deletion would silently export "no rulesets".
ruleset_ids=$(gh api "repos/$REPO/rulesets" --paginate --jq '.[].id')
# Build into a temp dir and swap only after every fetch succeeded — the
# committed baselines are never in a deleted-but-not-rewritten state.
# (File-existence doubles as the slug-collision check; no bash-4
# associative arrays — macOS ships bash 3.2.)
tmp_rulesets=$(mktemp -d)
for id in $ruleset_ids; do
  raw=$(gh api "repos/$REPO/rulesets/$id")
  name=$(jq -r '.name' <<<"$raw")
  slug=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g')
  if [ -z "$slug" ] || [ -e "$tmp_rulesets/ruleset-$slug.json" ]; then
    echo "ERROR: ruleset name '$name' produces an empty or colliding slug '$slug' — rename the ruleset" >&2
    exit 1
  fi
  jq -S -f scripts/normalize-ruleset.jq <<<"$raw" > "$tmp_rulesets/ruleset-$slug.json"
done
find "$OUT" -maxdepth 1 -name 'ruleset-*.json' -delete
# Zero live rulesets is a legitimate state to export (deliberate
# retirement) — without nullglob the unmatched glob would stay literal
# and abort the mv after the delete above (Codex, #190).
for f in "$tmp_rulesets"/ruleset-*.json; do
  [ -e "$f" ] || continue
  mv "$f" "$OUT/$(basename "$f")"
  echo "wrote $OUT/$(basename "$f")"
done
rmdir "$tmp_rulesets"

# --- 2. Public repo settings (CI drift-checked) ---
gh api "repos/$REPO" | jq -S -f scripts/normalize-repo-public.jq > "$OUT/repo-public.json"
echo "wrote $OUT/repo-public.json"

# --- 3. Admin-only snapshot (NOT CI drift-checked: needs `administration`
#        scope, which a workflow GITHUB_TOKEN cannot hold; refreshed here) ---
repo_json=$(gh api "repos/$REPO")

# Classic branch protection: only a 404 means "absent" — any other failure
# (403 scope, 5xx, rate limit) must abort, not be recorded as absence.
classic_err=$(mktemp)
if classic=$(gh api "repos/$REPO/branches/main/protection" 2>"$classic_err"); then
  classic=$(jq 'walk(if type == "object" then del(.url, .contexts_url) else . end)' <<<"$classic")
elif grep -q 'HTTP 404' "$classic_err"; then
  classic=null   # classic protection removed — record its absence
else
  echo "ERROR: could not read classic branch protection:" >&2
  cat "$classic_err" >&2
  rm -f "$classic_err"
  exit 1
fi
rm -f "$classic_err"

# Actions policy; the selected-actions allowlist is load-bearing when the
# policy is "selected", so its fetch must hard-fail rather than null out.
actions_permissions=$(gh api "repos/$REPO/actions/permissions" \
  | jq '{enabled, allowed_actions, sha_pinning_required}')
if [ "$(jq -r '.allowed_actions' <<<"$actions_permissions")" = "selected" ]; then
  selected_actions=$(gh api "repos/$REPO/actions/permissions/selected-actions")
else
  selected_actions=null
fi

# Bypass actors: fail-fast loop (a partial array must never look complete —
# empty bypass lists are precisely what this snapshot exists to prove).
bypass_tmp=$(mktemp)
for id in $ruleset_ids; do
  gh api "repos/$REPO/rulesets/$id" | jq '{name, bypass_actors}' >> "$bypass_tmp"
done
ruleset_bypass=$(jq -s 'sort_by(.name)' "$bypass_tmp")
rm -f "$bypass_tmp"

jq -n \
  --argjson merge "$(jq '{allow_merge_commit, allow_squash_merge, allow_rebase_merge,
      allow_auto_merge, allow_update_branch, delete_branch_on_merge,
      squash_merge_commit_title, squash_merge_commit_message,
      merge_commit_title, merge_commit_message,
      use_squash_pr_title_as_default}' <<<"$repo_json")" \
  --argjson security "$(jq '.security_and_analysis' <<<"$repo_json")" \
  --argjson actions_permissions "$actions_permissions" \
  --argjson selected_actions "$selected_actions" \
  --argjson workflow_permissions "$(gh api "repos/$REPO/actions/permissions/workflow")" \
  --argjson ruleset_bypass "$ruleset_bypass" \
  --argjson classic_protection_main "$classic" \
  --argjson inventory "$(jq -n \
      --argjson hooks "$(gh api "repos/$REPO/hooks" | jq 'length')" \
      --argjson deploy_keys "$(gh api "repos/$REPO/keys" | jq 'length')" \
      --argjson environments "$(gh api "repos/$REPO/environments" --jq '.total_count')" \
      --argjson actions_secrets "$(gh api "repos/$REPO/actions/secrets" --jq '.total_count')" \
      --argjson actions_variables "$(gh api "repos/$REPO/actions/variables" --jq '.total_count')" \
      --argjson dependabot_secrets "$(gh api "repos/$REPO/dependabot/secrets" --jq '.total_count')" \
      '{hooks: $hooks, deploy_keys: $deploy_keys, environments: $environments,
        actions_secrets: $actions_secrets, actions_variables: $actions_variables,
        dependabot_secrets: $dependabot_secrets}')" \
  '{merge: $merge, security_and_analysis: $security,
    actions_permissions: $actions_permissions,
    selected_actions: $selected_actions,
    workflow_permissions: $workflow_permissions,
    ruleset_bypass: $ruleset_bypass,
    classic_protection_main: $classic_protection_main,
    inventory: $inventory}' \
  | jq -S . > "$OUT/repo-admin-snapshot.json"
echo "wrote $OUT/repo-admin-snapshot.json"
