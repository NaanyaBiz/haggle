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
- AI/Claude infrastructure: `.claude/` with hooks, 5 subagents, 4 slash
  commands; `AGENTS.md` (canonical) with `CLAUDE.md` symlink; `scripts/wt`
  worktree helper.
- CI: hassfest, hacs validation, ruff/mypy/pytest matrix, semantic-release
  on tag.
- HACS metadata: `hacs.json`, `info.md`, placeholder `brand/icon.png`
  (replace before going public).

### Notes
- AGL portal client is stubbed; data path not wired. Tracked as Sprint 1.
- Repo is private until first working release; flip to public for HACS
  submission then.
