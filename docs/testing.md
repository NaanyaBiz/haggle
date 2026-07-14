# Test strategy

The strategy in one sentence: every defect class that has ever escaped to
production gets a named, pinned regression test; the trust boundary gets
fuzzed; the recorder seam gets exercised for real; and no stable ships
without recorded acceptance on a live HA instance.

## The four layers

| Layer | What | Where | When it runs |
|---|---|---|---|
| 1. Unit | Parsers, client, models, const — pure Python against anonymised fixtures | `tests/test_parser.py`, `test_agl_client.py`, `test_const.py`, `test_pinning.py` | Every PR + push (CI required check) |
| 2. Harness integration | Real HA core via `pytest-homeassistant-custom-component`: setup/unload, config flow, coordinator, sensors, diagnostics — recorder mocked at the boundary; PLUS `tests/test_recorder_statistics.py`, which runs the sum-chain scenarios against a **real** recorder (`recorder_mock`) because the mocked seam is where the v0.3.0 spike and #114 escaped | `tests/test_*.py` | Every PR + push (CI required check) |
| 3. Fuzz | Atheris totality fuzzing of `agl/parser.py` — the trust boundary for attacker-influenceable JSON (TLS pinning is warn-only) | `tests/fuzz/`, `fuzz.yml` | Weekly deep run + 120 s smoke on every PR (required check) |
| 4. Acceptance (beta dogfood) | Beta prerelease soaks on the maintainer's live HA + volunteer testers validating against AGL-app ground truth on GitHub issues | HACS beta channel; recorded per `docs/releasing.md` | Every beta → stable promotion |

A weekly `compat.yml` run additionally executes layer 1+2 against the
latest phcc/HA (including beta) for early upstream-breakage warning —
non-blocking, failures open a `ha-compat` issue.

## Required depth per change type

- **Parser / client / models change**: layer-1 tests against an anonymised
  fixture (see § Fixtures). New endpoint ⇒ new fixture + parser tests
  (AGENTS.md § Adding a New Endpoint). Any change also gets the PR fuzz
  smoke automatically.
- **Coordinator / statistics change**: layer-2 tests; if the change touches
  cumulative-sum semantics (baselines, rewindow, heal, per-band series),
  extend `tests/test_recorder_statistics.py` — mocked-recorder tests alone
  are NOT sufficient evidence for sum-chain changes.
- **Config flow / sensor / setup change**: layer-2 tests + manual test on a
  real HA instance before merge (PR checklist box).
- **Escaped defect (any severity)**: a named regression test pinned to the
  defect, cross-referenced in AGENTS.md. Existing pins:
  `test_uses_outer_consumption_quantity_not_inner_values` (v0.1.0
  undercount), `test_baseline_looked_up_at_earliest_fetched_hour` (v0.3.0
  spike), `test_band_reachback_baseline_after_long_absence` (#114),
  `test_feedin_reconciles_with_agl_app_figures` (solar reconciliation),
  `TestParserTotality` (fuzz crash classes, PR #177).
- **Docs / CI-only change**: green CI is the definition of done.

## Coverage floor

**89 % combined (lines + branches) coverage, gated** — `--cov-fail-under=89`
on the pytest command line in `ci.yml` is the enforcement point; this
document just names it. The floor lives in `ci.yml` deliberately, NOT in
`pyproject.toml` addopts: a local single-file run (`uv run pytest
tests/test_parser.py`) computes low coverage by construction and would
always trip an addopts-level floor. The floor is a ratchet — it moves UP
as the total rises (90.0 % at floor-setting time, 2026-07-13) and is never
lowered to make a PR pass (AGENTS.md § What NOT to Do). Do not chase the
number with assertion-free tests; the floor exists to stop silent erosion,
not to be a target.

## When live-HA manual testing is required

Before merge (PR checklist): any change to the config flow, sensor
definitions, or entity registry behaviour.
Before beta→stable promotion (always): soak per `docs/releasing.md`,
including Energy-dashboard-vs-AGL-app reconciliation.
Never required for: parser-only changes fully covered by fixtures,
docs, CI plumbing.

## Fixtures

Committed fixtures use the canonical anonymised placeholders
(`1234567890` / `9999999999` / `1 Sample Street SUBURB QLD 4000`).
The single exception is documented in `tests/fixtures/PROVENANCE.md`.
Never commit a capture with real customer identifiers.
