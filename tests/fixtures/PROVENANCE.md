# Fixture provenance

All fixtures in this directory are synthetic or anonymised to the canonical
placeholders (`1234567890` / `9999999999` / `1 Sample Street SUBURB QLD
4000`), with one documented exception:

## solar_hourly_response.json

This is a real full-day (2026-07-01) `ElectricitySolar` capture from the
maintainer's own AGL account. It contains no direct identifiers (no account
number, contract number, name, or address — verified), but the half-hourly
consumption/feed-in quantities are a genuine household meter timeseries.

It is kept real, not perturbed, deliberately: the regression test
`test_parser.py::TestParseSolarIntervals::test_feedin_reconciles_with_agl_app_figures`
proves the parser's field selection (outer `feedIn.quantity`/`amount`)
reconciles with the figures the AGL app displayed for this exact day
(8.02 kWh sold to grid / $1.36 credit). Perturbing the values would reduce
that test to checking arithmetic against itself.

**Consent**: I am the account holder for the meter behind this capture and
I consent to this single day of interval data being published in this
repository. — @naanyabiz, 2026-07-14

Rule for future fixtures: real captures require (1) zero direct
identifiers, (2) a documented reconciliation purpose that anonymised data
cannot serve, and (3) a consent entry here from the account holder.
Otherwise use the placeholders.
