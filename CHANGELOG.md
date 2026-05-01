# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial repo scaffolding: license, README, `.gitignore`, `.gitattributes`.
- Python tooling: `pyproject.toml` with ruff (strict), mypy (strict), pytest;
  `pre-commit` with ruff/mypy/gitleaks/commitlint; devcontainer.
- HA integration skeleton: `custom_components/haggle/` with `__init__.py`,
  `config_flow.py` (email + password + OTP), `coordinator.py`,
  `sensor.py`, `agl/client.py` (stubs).
- AI/Claude infrastructure: `.claude/` with hooks, 5 subagents, 5 slash
  commands (including `/pr`); `AGENTS.md` (canonical) with `CLAUDE.md`
  symlink; `scripts/wt` worktree helper.
- CI: hassfest, hacs validation, ruff/mypy/pytest matrix, semantic-release
  on tag.
- HACS metadata: `hacs.json`, `info.md`, placeholder `brand/icon.png`
  (replace before going public).
- AGL API client (`agl/client.py`): Auth0 JWT-based token refresh with
  proactive expiry check, token rotation persistence via callback,
  401-retry, and typed HTTP methods for overview, intervals, plan rates.
- AGL domain models (`agl/models.py`): typed dataclasses — `TokenSet`,
  `Contract`, `IntervalReading`, `DailyReading`, `BillPeriod`, `PlanRates`.
- AGL response parser (`agl/parser.py`): JSON → domain model conversions.
- Config flow: PKCE OAuth2 — generates `/authorize` URL, user pastes
  callback URL, code exchanged for tokens, contract selection for multiple
  contracts, reauth support.
- Coordinator: `HaggleData` typed dataclass, 30-day backfill on first
  install, incremental daily fetch, external statistics for Energy dashboard.
- 15 tests: PKCE config-flow path and AGL client unit tests with captured
  API response fixtures.

### Notes
- Repo is private until first working release; flip to public for HACS
  submission then.
