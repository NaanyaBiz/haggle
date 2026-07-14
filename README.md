# haggle

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=NaanyaBiz&repository=haggle&category=integration)
[![Latest release](https://img.shields.io/github/v/release/NaanyaBiz/haggle?include_prereleases&label=latest)](https://github.com/NaanyaBiz/haggle/releases)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/NaanyaBiz/haggle/badge)](https://scorecard.dev/viewer/?uri=github.com/NaanyaBiz/haggle)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/13582/badge)](https://www.bestpractices.dev/projects/13582)

Home Assistant custom integration that pulls smart-meter usage from
[AGL Australia](https://www.agl.com.au/) and feeds it into the HA Energy
dashboard.

> **Unofficial community integration.** Not affiliated with, endorsed by, or
> supported by AGL Energy Limited. The API contract used by this integration is
> not publicly documented and may change at any time; any breakage will be
> addressed on a best-effort basis.
>
> **Australia only.** Requires an AGL Energy electricity account with a smart
> meter (most Australian metropolitan installations).

> **Status** (current version: see the badge above or the
> [releases page](https://github.com/NaanyaBiz/haggle/releases)): the
> flat-rate consumption/cost path is stable and runs live in the maintainer's
> Home Assistant. **Time-of-Use** support is in validation with real ToU
> customers ([#126](https://github.com/NaanyaBiz/haggle/issues/126)), and
> **solar feed-in** support is in beta validation
> ([#128](https://github.com/NaanyaBiz/haggle/issues/128)) — enable
> "Show beta versions" in HACS to try either. See
> [`CHANGELOG.md`](./CHANGELOG.md) for milestone detail.

## Why

AGL is one of Australia's largest electricity retailers. Their mobile app
exposes half-hourly interval data for smart-meter accounts. `haggle` uses the
same authenticated API endpoints that AGL's own mobile clients use, fetches
your smart-meter intervals, and feeds them into Home Assistant's long-term
statistics so the Energy dashboard shows your usage on the hours it actually
happened.

## Install

Haggle is in the [HACS](https://hacs.xyz/) default store:

1. HACS → search **Haggle** → *Download*, then restart Home Assistant.
   (Or click the badge above on a machine with
   [My Home Assistant](https://my.home-assistant.io/) configured.)
2. *Settings* → *Devices & Services* → *Add integration* → search **AGL Haggle**.
3. A login URL is shown. Open it in your **real browser** (handles Akamai
   bot-protection and MFA transparently). Complete your AGL login.
4. After login, AGL redirects to a "Not Found" page. Copy the full URL from
   your browser's address bar and paste it into the HA dialog.
5. If you have multiple electricity contracts, select the one to monitor.

The integration will backfill 30 days of history on first run (throttled to
7 days per daily poll), then poll once per day. AGL interval data lags
24–48 h.

## Removing

Delete the Haggle entry from **Settings → Devices & Services**. On removal the
integration makes a best-effort call to AGL's sign-in service (Auth0) to revoke its
stored refresh token, so the grant does not outlive the install. If removal happens
while offline — or you deleted the files without removing the entry, or restored a
backup containing an old entry — revoke the session yourself in your AGL account's
security settings. Your imported `haggle:*` statistics are deliberately kept (they are
your own meter history); prune them via Developer Tools → Statistics if you want them
gone.

## Rollback / downgrade

Any prior release (except yanked ones) can be reinstalled:
**HACS → Haggle → ⋮ → Redownload → pick the version → restart HA.**

- **Config entries are downgrade-safe.** Newer versions only *add* keys
  to the stored entry (e.g. the solar-heal record, TLS-pin hashes);
  older versions ignore keys they don't know, and the entry schema
  version has never changed. No re-setup needed in either direction.
- **Statistics survive.** Downgrading never requires touching the
  recorder: imports are idempotent, so the older version simply
  overwrites the trailing rewindow with its own (identical) sums. A
  statistics wipe is only needed when the release notes for the version
  you are *leaving* say it wrote corrupted sums — the procedure (delete
  the `haggle:*` rows from `statistics` / `statistics_short_term`, let
  the 30-day backfill rebuild) is in the CHANGELOG entries that need it.
- **Solar caveat**: downgrading below v0.4.0 freezes the
  `haggle:generation_*` series (history stays, no new rows). On
  re-upgrade the gap backfills automatically.
- **Outage tolerance**: AGL data lags 24–48 h anyway and every poll
  re-fetches the trailing 7 days on top of a 30-day backfill floor —
  an outage or rollback window of hours-to-days loses no data.

Each stable release records a manual downgrade test to the previous
stable in its release PR (see `docs/releasing.md`).

## Energy dashboard

**Add the `haggle:…` statistics as your dashboard sources — never the
`sensor.…` entities.** The statistics carry your real half-hourly history;
the sensor entities update once per day and would render a whole day as one
bar on the wrong date.

- **Flat rate:** add `haggle:consumption_<contract>` as *Grid consumption*.
- **Time-of-Use:** add only the per-tariff series (peak / off-peak /
  shoulder / anytime) — not the aggregate as well, or every kWh counts twice.
- **Solar:** additionally add `haggle:generation_<contract>` as
  *Return to grid*.

Full setup guide, sensor glossary, data-timing expectations, and
troubleshooting: **[docs/energy-dashboard.md](./docs/energy-dashboard.md)**.

## Time-of-Use (ToU)

On a **Time-of-Use** contract — where AGL tags each 30-minute interval as
`peak`, `off-peak`, or `shoulder` — `haggle` writes a separate consumption +
cost statistic per tariff band (peak / off-peak / shoulder / anytime), each
selectable as its own Energy-dashboard source, plus per-band unit-rate
sensors. The split is driven entirely by AGL's per-interval tariff tags.

> ToU is in validation with real AGL ToU customers. The usage/cost split
> follows AGL's documented per-interval tags; the per-band **rate sensors**
> infer their band from free-text plan wording and may read `unavailable`
> ([#90](https://github.com/NaanyaBiz/haggle/issues/90)). Locally-derived
> tariff windows are planned
> ([#141](https://github.com/NaanyaBiz/haggle/issues/141)).

## Solar (feed-in)

On contracts with rooftop solar, `haggle` also fetches your export data and
writes `haggle:generation_<contract>` (exported kWh — an Energy-dashboard
*Return to grid* source) and `haggle:generation_credit_<contract>` (feed-in
credit). Sensors cover the current billing period (matching the AGL app's
"Sold To Grid" tile), cumulative totals, and your feed-in rate.

> Solar is in beta validation
> ([#128](https://github.com/NaanyaBiz/haggle/issues/128)) — the field
> mapping is confirmed against a real capture and the AGL app; install the
> latest pre-release via HACS "Show beta versions" to try it.

## Develop

See [`AGENTS.md`](./AGENTS.md) for the full dev workflow, subagent index,
and AGL API contract documentation.

## Provenance

This codebase is generated by [Claude Code](https://claude.com/claude-code)
and reviewed by a human. Every commit carries a `Co-Authored-By: Claude`
trailer, enforced by a pre-commit hook.

## License

Apache-2.0 — see [`LICENSE`](./LICENSE).
