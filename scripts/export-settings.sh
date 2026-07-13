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
set -euo pipefail

REPO=${1:-NaanyaBiz/haggle}
cd "$(git rev-parse --show-toplevel)"
OUT=.github/settings
mkdir -p "$OUT"

# --- 1. Rulesets: one normalized file per live ruleset (CI drift-checked) ---
find "$OUT" -maxdepth 1 -name 'ruleset-*.json' -delete
for id in $(gh api "repos/$REPO/rulesets" --paginate --jq '.[].id'); do
  raw=$(gh api "repos/$REPO/rulesets/$id")
  name=$(jq -r '.name' <<<"$raw")
  slug=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g')
  jq -S -f scripts/normalize-ruleset.jq <<<"$raw" > "$OUT/ruleset-$slug.json"
  echo "wrote $OUT/ruleset-$slug.json"
done

# --- 2. Public repo settings (CI drift-checked) ---
gh api "repos/$REPO" | jq -S -f scripts/normalize-repo-public.jq > "$OUT/repo-public.json"
echo "wrote $OUT/repo-public.json"

# --- 3. Admin-only snapshot (NOT CI drift-checked: needs `administration`
#        scope, which a workflow GITHUB_TOKEN cannot hold; refreshed here) ---
repo_json=$(gh api "repos/$REPO")
if classic=$(gh api "repos/$REPO/branches/main/protection" 2>/dev/null); then
  classic=$(jq 'walk(if type == "object" then del(.url, .contexts_url) else . end)' <<<"$classic")
else
  classic=null   # classic protection removed — record its absence
fi
jq -n \
  --argjson merge "$(jq '{allow_merge_commit, allow_squash_merge, allow_rebase_merge,
      allow_auto_merge, allow_update_branch, delete_branch_on_merge,
      squash_merge_commit_title, squash_merge_commit_message,
      merge_commit_title, merge_commit_message,
      use_squash_pr_title_as_default}' <<<"$repo_json")" \
  --argjson security "$(jq '.security_and_analysis' <<<"$repo_json")" \
  --argjson actions_permissions "$(gh api "repos/$REPO/actions/permissions" \
      | jq '{enabled, allowed_actions, sha_pinning_required}')" \
  --argjson selected_actions "$(gh api "repos/$REPO/actions/permissions/selected-actions" 2>/dev/null || echo null)" \
  --argjson workflow_permissions "$(gh api "repos/$REPO/actions/permissions/workflow")" \
  --argjson ruleset_bypass "$(gh api "repos/$REPO/rulesets" --paginate --jq '.[].id' \
      | while read -r id; do gh api "repos/$REPO/rulesets/$id" | jq '{name, bypass_actors}'; done \
      | jq -s 'sort_by(.name)')" \
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
