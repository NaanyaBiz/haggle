## Summary

<!-- 1-3 bullet points describing what this PR does and why -->

-
-

## Test plan

- [ ] `uv run pytest` passes
- [ ] `uv run ruff check . && uv run ruff format --check .` clean
- [ ] `uv run mypy custom_components/haggle` clean
- [ ] Hassfest passes locally (or CI hassfest check is green)
- [ ] Manual test on a real HA instance (if touching config flow or sensors)
- [ ] All commits are signed and show **Verified** on GitHub
      (`CONTRIBUTING.md → Sign your commits`)

## AI generation disclosure

- [ ] AI tool(s) that generated or co-authored this PR: ______ (or "none").
      Every commit carries a matching `Co-Authored-By: <tool> <email>`
      trailer, or `AI-Assisted: none` (`CONTRIBUTING.md → Commit conventions`).
- [ ] The human maintainer has reviewed and understands every change.

<!--
Checklist discipline: before merge, every box above must be either ticked
or deleted with a one-line reason inline. The "human maintainer has
reviewed" box is this repo's core provenance claim — a PR must not merge
with it unchecked.
-->
