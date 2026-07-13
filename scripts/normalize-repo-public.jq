# Project GET /repos/{owner}/{repo} down to the settings-like fields that are
# visible WITHOUT authentication on a public repo (verified 2026-07-13:
# identical output from an admin-token and an anonymous response). Volatile
# fields (counts, timestamps, description, topics) are deliberately excluded.
# Admin-gated fields (merge methods, security_and_analysis) live in
# repo-admin-snapshot.json.
{
  default_branch,
  visibility,
  archived,
  disabled,
  is_template,
  fork,
  allow_forking,
  web_commit_signoff_required,
  pull_request_creation_policy,
  has_issues,
  has_projects,
  has_wiki,
  has_discussions,
  has_pages,
  has_downloads,
  license: (.license.spdx_id // null)
}
