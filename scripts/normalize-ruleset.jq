# Normalize a GitHub ruleset export to a stable, viewer-independent form.
# Keeps only declarative fields readable by ANY token (including the Actions
# GITHUB_TOKEN and anonymous callers); strips ids, timestamps, links, and
# viewer-dependent fields: bypass_actors is only returned to callers with
# write access to the ruleset, and current_user_can_bypass varies by caller.
# bypass_actors is recorded in repo-admin-snapshot.json instead.
# Arrays with set semantics are sorted so exports are diff-stable.
{
  name: .name,
  target: .target,
  enforcement: .enforcement,
  conditions: (.conditions // {}
    | with_entries(.value |= (
        if type == "object"
        then with_entries(.value |= (if type == "array" then sort else . end))
        else . end))),
  rules: (
    .rules
    | map(if .type == "required_status_checks"
          then .parameters.required_status_checks |= sort_by(.context)
          elif .type == "pull_request"
          then .parameters.allowed_merge_methods |= sort
          else . end)
    | sort_by(.type)
  )
}
