#!/usr/bin/env bash
# Quarterly access review for NaanyaBiz/haggle (SECURITY.md "Access Review").
#
# Read-only: asserts the expected access surface via the maintainer's local
# `gh` auth and exits non-zero on drift. Deliberately NOT run in CI — the
# deploy-key/webhook endpoints need admin scope, and storing an admin PAT
# as an Actions secret would break the repo's zero-standing-secrets
# invariant (see SECURITY.md). Complements the weekly settings-drift
# workflow, which diffs the settings-as-code baselines in .github/settings/.
#
# Usage: ./scripts/access-review.sh
set -euo pipefail

REPO="NaanyaBiz/haggle"
EXPECTED_ADMIN="NaanyaBiz"
FAILURES=0

check() { # check <label> <actual> <expected>
  local label="$1" actual="$2" expected="$3"
  if [[ "${actual}" == "${expected}" ]]; then
    printf 'PASS  %-34s %s\n' "${label}" "${actual}"
  else
    printf 'FAIL  %-34s got=%s want=%s\n' "${label}" "${actual}" "${expected}"
    FAILURES=$((FAILURES + 1))
  fi
}

ruleset_id() { # ruleset_id <name> — prints the id, or nothing if absent
  gh api "repos/${REPO}/rulesets" --paginate \
    --jq ".[] | select(.name==\"$1\") | .id"
}

echo "== Access review: ${REPO} — $(date -u +%Y-%m-%dT%H:%MZ) =="

echo
echo "-- Collaborators / keys / hooks --"
collabs="$(gh api "repos/${REPO}/collaborators" --jq '[.[].login] | sort | join(",")')"
check "collaborators" "${collabs}" "${EXPECTED_ADMIN}"
check "pending collaborator invitations" "$(gh api "repos/${REPO}/invitations" --jq 'length')" "0"
check "deploy keys" "$(gh api "repos/${REPO}/keys" --jq 'length')" "0"
check "webhooks" "$(gh api "repos/${REPO}/hooks" --jq 'length')" "0"

echo
echo "-- Zero standing secrets --"
check "actions secrets" "$(gh api "repos/${REPO}/actions/secrets" --jq '.total_count')" "0"
check "actions variables" "$(gh api "repos/${REPO}/actions/variables" --jq '.total_count')" "0"
check "dependabot secrets" "$(gh api "repos/${REPO}/dependabot/secrets" --jq '.total_count')" "0"
check "environments" "$(gh api "repos/${REPO}/environments" --jq '.total_count')" "0"

echo
echo "-- Rulesets (single control source — classic protection stays deleted) --"
MAIN_ID=""
for name in protect-main protect-release-tags; do
  id="$(ruleset_id "${name}")"
  if [[ -z "${id}" ]]; then
    check "ruleset ${name}" "absent" "present"
    continue
  fi
  [[ "${name}" == "protect-main" ]] && MAIN_ID="${id}"
  detail="$(gh api "repos/${REPO}/rulesets/${id}")"
  check "${name} enforcement" "$(jq -r '.enforcement' <<<"${detail}")" "active"
  # bypass_actors is only returned to callers with ruleset write access —
  # a lesser token must FAIL this check, not sail through on a missing key.
  check "${name} bypass actors" "$(jq -r 'if has("bypass_actors") then (.bypass_actors | length | tostring) else "UNREADABLE (token lacks ruleset write access)" end' <<<"${detail}")" "0"
done
if gh api "repos/${REPO}/branches/main/protection" >/dev/null 2>&1; then
  check "classic protection on main" "present" "absent"
else
  check "classic protection on main" "absent" "absent"
fi

echo
echo "-- Actions policy --"
actions="$(gh api "repos/${REPO}/actions/permissions")"
check "allowed actions" "$(jq -r '.allowed_actions' <<<"${actions}")" "selected"
check "SHA pinning required" "$(jq -r '.sha_pinning_required' <<<"${actions}")" "true"
sel="$(gh api "repos/${REPO}/actions/permissions/selected-actions")"
check "github-owned actions allowed" "$(jq -r '.github_owned_allowed' <<<"${sel}")" "true"
check "verified-creator blanket allow" "$(jq -r '.verified_allowed' <<<"${sel}")" "false"
check "publisher allowlist (exact)" "$(jq -r '.patterns_allowed | sort | join(",")' <<<"${sel}")" \
  "astral-sh/setup-uv@*,hacs/action@*,home-assistant/actions/*,ossf/scorecard-action@*"
wf_perms="$(gh api "repos/${REPO}/actions/permissions/workflow" \
  --jq '.default_workflow_permissions + "/" + (.can_approve_pull_request_reviews|tostring)')"
check "default workflow token" "${wf_perms}" "read/false"

echo
echo "-- State (informational — declared copies live in .github/settings/) --"
if [[ -n "${MAIN_ID}" ]]; then
  echo "protect-main rules:"
  gh api "repos/${REPO}/rulesets/${MAIN_ID}" --jq '.rules[].type' | sed 's/^/  - /'
  echo "protect-main required checks:"
  gh api "repos/${REPO}/rulesets/${MAIN_ID}" \
    --jq '.rules[] | select(.type=="required_status_checks")
          | .parameters.required_status_checks[].context' | sed 's/^/  - /'
fi
echo "selected-actions allowlist:"
gh api "repos/${REPO}/actions/permissions/selected-actions" \
  --jq '((if .github_owned_allowed then ["<github-owned>"] else [] end)
        + .patterns_allowed)[]' | sed 's/^/  - /'

echo
echo "== Manual checklist (not queryable with the CLI token — API limits) =="
echo "  [ ] Account MFA still enforced:       https://github.com/settings/security"
echo "  [ ] Review authorized OAuth apps:     https://github.com/settings/applications"
echo "  [ ] Review GitHub App installations:  https://github.com/settings/installations"
echo "  [ ] Prune stale personal tokens:      https://github.com/settings/tokens"
echo "  [ ] SSH + signing keys match the expected set (the security@naanya.biz"
echo "      ed25519 signing key + known access keys); remove anything unexpected:"
echo "                                        https://github.com/settings/keys"
echo "  [ ] Record this run (date + result) on the standing access-review issue."

if [[ "${FAILURES}" -gt 0 ]]; then
  echo
  echo "ACCESS REVIEW FAILED: ${FAILURES} drift(s) — investigate before dismissing."
  exit 1
fi
echo
echo "Access surface matches the declared state."
