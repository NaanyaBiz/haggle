# Fixture provenance

All fixtures in this directory are synthetic or anonymised to the canonical
placeholders (`1234567890` / `9999999999` / `1 Sample Street SUBURB QLD
4000`), with one documented exception:

## solar_hourly_response.json

This is a real full-day (2026-07-01) `ElectricitySolar` capture from the
AGL account of the user who requested solar support — contributed
voluntarily and publicly by **@kaizersoje** on
[#128](https://github.com/NaanyaBiz/haggle/issues/128) (2026-07-06
comment, alongside the matching AGL-app figures for that day). It contains
no direct identifiers (no account number, contract number, name, or
address — verified before commit; the contract number in the accompanying
URL was zeroed by the contributor themselves), but the half-hourly
consumption/feed-in quantities are a genuine household meter timeseries.

It is kept real, not perturbed, deliberately: the regression test
`test_parser.py::TestParseSolarIntervals::test_feedin_reconciles_with_agl_app_figures`
proves the parser's field selection (outer `feedIn.quantity`/`amount`)
reconciles with the figures the contributor's AGL app displayed for this
exact day (8.02 kWh sold to grid / $1.36 credit). Perturbing the values
would reduce that test to checking arithmetic against itself.

**Consent basis**: the account holder personally published this capture
and the reference figures on the public issue tracker for the stated
purpose of building and validating this integration; that contribution —
permanently linked above — is the consent record. It was not solicited as
a committed fixture at the time, so if @kaizersoje ever asks for its
removal, it will be replaced with a synthetic fixture and the
reconciliation test retired to an unverifiable-claims comment.

**What this capture does and does not evidence.** The contributor's state
is not recorded, and the capture cannot tell us: its first slot
(`2026-06-30T14:00:00Z`) is consistent with any eastern state in winter AND
with a South Australian contract under the Sydney-conversion bug
([#292](https://github.com/NaanyaBiz/haggle/issues/292)). It is an
eastern-winter capture of unknown state that evidences field selection
only — never cite it for any timezone claim.

## Timestamp-convention evidence (#292) — no committed fixture

AGL converts every meter's local slot labels to UTC through
`Australia/Sydney` regardless of the contract's zone (#292). Neither
confirmation lives in this directory: the Queensland evidence is the
maintainer's own raw captures from both DST seasons (held locally, never
committed — they carry a real contract number and a residence's meter
series), and the South Australian evidence is the reporter's 887-hour
reconciliation on the issue. The #292 tests build minimal in-test payloads
instead; a synthetic SA fixture would be the #13 fixture-as-fact failure
class and is deliberately absent. A real, anonymised one-day SA capture has
been requested from the reporter on #292; when it arrives, commit it under
the rule below with the issue comment as its consent record and add a
reconciliation test (first raw slot `14:00:00Z` / `13:00:00Z` → corrected
contract-local midnight, 48 slots, daily kWh equal to the reporter's CSV
figure).

Rule for future fixtures: real captures require (1) zero direct
identifiers, (2) a documented reconciliation purpose that anonymised data
cannot serve, and (3) a provenance entry here linking the account
holder's own public contribution (or recording their explicit consent).
Otherwise use the placeholders.
