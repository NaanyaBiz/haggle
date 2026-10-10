# Energy dashboard setup

Haggle feeds your AGL smart-meter history into Home Assistant as **external
statistics** — series named `haggle:…` that carry your real half-hourly usage,
written to the hours the energy was actually used. These, not the sensor
entities, are what belongs in the Energy dashboard.

> **The one rule:** in the Energy dashboard, always pick a `haggle:…`
> statistic (display names start with "AGL …"). Never pick a `sensor.…`
> entity. The sensor entities update once per daily poll, so the dashboard
> would attribute a whole day's energy to the poll hour — one big bar, on the
> wrong day (see [Troubleshooting](#troubleshooting) below).

## Which sources to add

Find your exact statistic IDs under **Developer Tools → Statistics**
(filter "haggle"). `<contract>` below is your AGL contract number.

| Your plan | Grid consumption | Return to grid (solar) | Do **not** add |
|---|---|---|---|
| **Flat rate** | `haggle:consumption_<contract>` — "AGL Electricity Consumption (…)" | — | any `sensor.…` entity |
| **Time-of-Use** | the per-tariff series only: `haggle:consumption_peak_…`, `…_offpeak_…`, `…_shoulder_…`, `…_normal_…` ("AGL Electricity Consumption Peak/Off-Peak/Shoulder/Anytime (…)") | — | the aggregate `haggle:consumption_<contract>` **as well** — the per-tariff series are a partition of it, so adding both counts every kWh twice |
| **Solar** (either plan type) | as above for your plan type | `haggle:generation_<contract>` — "AGL Solar Generation (…)" | any `sensor.…` entity |

Each consumption series has a matching cost series (`haggle:cost_…`,
`haggle:generation_credit_…`) if you use the dashboard's cost tracking.

## What the sensor entities are for

The sensors are for at-a-glance values, dashboards cards, and automations —
not for the Energy dashboard:

| Sensor | What it shows |
|---|---|
| **Consumption** | Cumulative kWh ever imported by the integration. Mirrors the statistics total; moves once per daily poll. Not selectable in the Energy dashboard (de-listed, [#147](https://github.com/NaanyaBiz/haggle/issues/147)). |
| **Consumption this period** | kWh so far in the current AGL billing period (matches the app's "Usage So Far"). Not selectable in the Energy dashboard (de-listed, [#147](https://github.com/NaanyaBiz/haggle/issues/147)). |
| **Consumption cost** | AUD so far in the current billing period. |
| **Bill projection** | AGL's own forecast for the current bill, read from the account overview. **Solar contracts:** AGL uses that slot for "Sold To Grid" instead, so no projection is available and this sensor stays `unknown` — it deliberately shows nothing rather than the wrong number ([#253](https://github.com/NaanyaBiz/haggle/issues/253)). Read `unknown` on a non-solar contract in v0.4.0 and earlier; fixed in v0.5.0. |
| **Unit rate / Supply charge** | Your plan's c/kWh (as AUD/kWh) and daily supply charge. |
| **Unit rate (peak / off-peak / shoulder)** | Per-band rates — ToU contracts only. |
| **Solar generation / Solar feed-in credit** | Cumulative exported kWh / credited AUD ever imported — solar contracts only. Like **Consumption**, these are running totals: do not compare them to the app's billing-period tile. **Solar generation** is not selectable in the Energy dashboard (de-listed, [#147](https://github.com/NaanyaBiz/haggle/issues/147)). |
| **Solar sold this period / Solar feed-in credit this period** | Exported kWh / credited AUD in the current billing period — these are the numbers that match the AGL app's "Sold To Grid" tile. **Solar sold this period** is not selectable in the Energy dashboard (de-listed, [#147](https://github.com/NaanyaBiz/haggle/issues/147)). Caveat: on **quarterly billing**, a period that started more than 30 days ago predates the backfill window, so these cover only the stored 30-day history and will read lower than the app until a new period starts. |
| **Solar feed-in rate** | Your feed-in tariff in AUD/kWh. |

## Data timing — what "normal" looks like

- **AGL's feed lags 24–48 hours.** Today is always empty; yesterday often
  arrives a day late. This is AGL/AEMO, not the integration.
- **First install backfills 30 days**, throttled to 7 days per daily poll —
  so the full month of history takes up to ~4–5 days to appear.
- **Solar period sensors start as `unknown`** and stay that way until the
  generation history has caught up (up to ~4 daily polls after
  installing/upgrading). They deliberately never show a partial number.
- Every poll re-fetches the trailing 7 days, so slots AGL fills in late are
  corrected automatically.

## Troubleshooting

Suspect a recently-updated Haggle version rather than your dashboard
config? Any prior release can be reinstalled safely — see
[Rollback / downgrade](../README.md#rollback--downgrade) in the README.

**A whole day shows as one big bar, on the wrong day.**
The dashboard is charting a `sensor.…` entity instead of the `haggle:…`
statistic. Remove the sensor from the dashboard's sources and add the
statistic (see the table above). Past days re-render correctly immediately —
the statistic already holds the hourly history. The kWh total sensors
(cumulative **and** *this period*, consumption **and** solar) are no longer
offered in the Energy source picker
([#147](https://github.com/NaanyaBiz/haggle/issues/147)), so new setups can't
hit this. On an install that had *already* charted one of these sensors, Home
Assistant raises a one-time **"state class removed"** repair — **resolve** it
(or delete that sensor's statistics under *Developer Tools → Statistics*) to
clear the now-stale source from the picker; dismissing the repair alone leaves
the old statistic selectable. (Reported as
[#137](https://github.com/NaanyaBiz/haggle/issues/137).)

**My daily kWh is exactly double the AGL app.**
Both the aggregate consumption series and the per-tariff series are added.
They overlap by design — keep only the per-tariff series (ToU) or only the
aggregate (flat rate).

**Solar sensors don't match the AGL app.**
Compare like with like: the *period* sensors match the app's billing-period
tile; the Energy dashboard's daily "Return to grid" bars match the app's
daily solar view. The cumulative **Solar generation** sensor is a running
total over all imported history and will never match a billing-period figure.
On quarterly billing the period sensors under-read once the billing period is
older than the 30-day backfill window (see the glossary above).

**Hourly bars are 30 minutes (SA / Broken Hill) or one hour (Queensland,
October–April) early — or, on v0.5.0-beta.1/beta.2, the first half-hour (SA /
Broken Hill) or first hour (Queensland) of every day is missing.**
AGL's API converts every meter's local slot labels to UTC through Sydney
time, wherever the meter actually is
([#292](https://github.com/NaanyaBiz/haggle/issues/292)). From
v0.5.0-beta.4 (see the CHANGELOG), slots are re-localised to your
contract's zone before import.

v0.5.0-beta.3 carried the same fix but was withdrawn. On its first poll it
could leave a few old rows behind, drawn as a **negative bar followed by an
inflated one**, typically around midday on a solar home or overnight on the
solar export series
([#300](https://github.com/NaanyaBiz/haggle/issues/300)). If you saw that,
upgrade to beta.5 or later. Each poll repairs the trailing week it
fetches again, and the 30-day re-alignment below covers older rows. There is
nothing to delete.

What gets repaired:

- **The trailing 7 days** are rewritten correctly on the first poll after
  upgrading. Rows are overwritten in place, with no doubled or negative bars.
- **From v0.5.0-beta.5, the 30 days before your upgrade** are re-aligned
  once, automatically (next entry).
- **History older than that** keeps the old timing: 30 min early for SA all
  year, 1 h early for Queensland across the October–April span. A Repairs
  notice tells you if you have such rows.

**Do not delete the `haggle:*` statistics to force a rebuild.** Haggle
backfills only 30 days, so a rebuild restores one month and loses
everything older for good. Nothing was ever missing or counted twice;
only the hour each slot sat in was off, so a reading near midnight could
show on the previous day. NSW, Victoria, Tasmania and the ACT were
never affected. Northern Territory and Western Australia are a separate
case; see the NT/WA entry below.

Two side effects of correct alignment are expected:

- On a time-of-use plan, the hours spanning a tariff boundary (e.g. 00:00,
  06:00, 10:00, 15:00 local) now show a mixed cost per kWh. That is
  correct: half the hour is on each rate.
- On an SA / Broken Hill / NT contract, the bar at local midnight belongs
  half to each day, because the hour straddles the day boundary.

The tariff *labels* AGL attaches to each slot are a separate matter
([#141](https://github.com/NaanyaBiz/haggle/issues/141)). To check
alignment, compare the hourly bars against the AGL portal's CSV export read
as **your** local time, not against the per-period sensors.

**After upgrading to v0.5.0-beta.5, the last month of hourly bars moved
(SA, Broken Hill, Queensland): the one-off timezone history re-alignment.**
This is expected and happens once. On the first poll after upgrading,
Haggle fetches the 30 days before today again and writes them back at the
correct hours. Rows are overwritten in place, and the month's total does
not change (a reading near midnight may move to its correct day).

- **Who gets it.** Your contract runs it only if its zone (from your
  service address) differs from Sydney time somewhere in those 30 days,
  and never by more than one hour. That covers SA and Broken Hill always,
  and Queensland when the 30 days include Sydney daylight-saving days.
  NSW, Victoria, Tasmania and the ACT do nothing. So do new installs, which
  have no older rows yet. An entry still catching up a backfill gap starts
  once its stored history reaches the 30-day window.
- **How long.** One poll for consumption (with cost and any time-of-use
  bands). On a solar contract, one more poll for solar export and feed-in
  credit. Each of these polls makes up to about 40 requests to AGL instead of
  the usual 10–19. During the solar poll, the "Solar sold this period" and "Solar
  feed-in credit this period" sensors may show `unknown` until it finishes.
- **Where the moved rows meet older rows**, nothing is counted twice. The
  first day of the re-aligned month totals exactly, with no extra bump.
  - *Known exception on solar contracts:* the solar export series may stay
    high by one early-morning export slot (up to an hour in Queensland). The
    slot is on the day your first beta.3/beta.4 poll's trailing week began,
    so the "this period" solar sensors include it while your bill period
    started on or before that day.
  - Haggle logs one WARNING, "AGL returned less energy than is stored",
    for that day during the solar poll. This is expected; there are no
    negative bars.
- **If AGL errors or rate-limits.** An interrupted or failed attempt writes
  only the normal trailing week and leaves the rest of the month as it
  was. Nothing is half-moved, missing or counted twice. An AGL error on
  an older day is retried at the next scheduled poll; errors on the six
  most recent days do not count, because the normal refresh fetches those
  days again anyway. A rate limit retries after 30 minutes, but only one
  rate-limited attempt counts per poll interval, and the third one counts
  as a failed attempt. After 3 failed attempts a stage gives up and keeps
  the old timing for that month. If consumption gave up, the solar stage
  still runs. Haggle then raises one Repairs issue that names what gave up.
  It is safe to dismiss. Do not delete statistics.
- **The history notice.** If rows *older* than the re-aligned month may
  still have the old timing, a Repairs notice appears once and never comes
  back after you dismiss it. Those rows stay as they are. Nothing is
  missing or counted twice; only the hour each slot sits in is early, so a
  reading near midnight can show on the previous day.
  - If you set this entry up on **0.5.0-beta.3 or later**, all of your
    history already has the correct timing. Your entry still runs the
    re-alignment once, and nothing changes, but you may get this notice
    too. Dismiss it. (In one unlikely case, a stored hour just before the
    month that happens to match the old-timing slots exactly, one
    half-hour (SA) or hour (Queensland) on the month's first day goes
    uncounted. There is no negative bar.)
- **Queensland timing.** If you upgrade by 3 November 2026, the month
  reaches back to 4 October, when Sydney's daylight saving began. That
  repairs all affected Queensland history. Upgrade later and the days from
  4 October to the start of the month keep the old timing; the notice
  covers them.
- **Solar statistics turned off.** If solar statistics writes are off in
  the integration's Options when the solar poll is due, solar history is
  not re-aligned, even if you turn writes back on later.
- **Checking progress in diagnostics.** Download diagnostics (see
  [docs/diagnostics.md](./diagnostics.md)) and look at `tz_realign`:

  | What you see | Meaning |
  |---|---|
  | `state: "pending"`, `series: "consumption"` | Consumption is next or in progress. |
  | `state: "pending"`, `series: "generation"` | Solar is next. |
  | `attempts` above 0 | Failed attempts so far for this stage, out of 3. |
  | `halts` above 0 | Rate-limited or interrupted attempts counted so far (at most one per poll interval). The third counts as one failed attempt. |
  | `state: "done"`, `reason: "complete"` | Finished. `notice: true` means the older-history notice was raised. `generation: "skipped"` means solar had nothing to move, or solar writes were off. |
  | `reason: "gave_up"` | At least one stage stopped after 3 failed attempts. `consumption` / `generation` say which (`"gave_up"`). That stage's month keeps its old timing, with totals intact. |
  | `reason: "not_affected"` | Nothing needed moving. |
  | `reason: "fresh_install"` | Nothing needed moving. |
  | `reason: "unsupported_shift"` | Northern Territory or Western Australia (next entry). |

**Northern Territory or Western Australia: older hourly bars are still
early, and no Repairs notice appeared.**
This is a known, recorded limitation (`SECURITY.md` RA-19). AGL converts
NT and WA timestamps through Sydney time too, and from v0.5.0-beta.3 new
data is corrected. The one-off history re-alignment is built and tested
only for shifts of up to one hour, though. NT's shift is 90 minutes while
Sydney is on daylight saving (30 minutes otherwise), and WA's is 2 hours
(3 during Sydney daylight saving).

Haggle therefore skips the re-alignment for these contracts. It logs one
INFO line, and diagnostics show `tz_realign.reason: "unsupported_shift"`.
A brand-new install shows `"fresh_install"` instead; it has no old rows.
No Repairs notice is raised.

What you see:

- History written before v0.5.0-beta.3 keeps the old timing: NT 30 or 90
  minutes early, WA 2 or 3 hours early. Nothing is missing or counted
  twice, but readings from the first hours after midnight show on the
  previous day.
- In the trailing week that your first corrected poll rewrote, a few
  old-timing rows may remain next to the corrected ones. Those hours can
  read high. That poll logged a WARNING, "AGL returned less energy than is
  stored". There are no negative bars.
- The decision is made once, from the largest shift in the 30 days before
  the first poll on beta.5 or later. WA is always excluded. NT is excluded
  whenever those 30 days include a Sydney daylight-saving day, which is
  true of any NT upgrade in October 2026. An NT entry first evaluated while
  the whole month is in Sydney standard time (roughly 5 May to 4 October)
  has only a 30-minute shift and *is* re-aligned like SA.

AGL does not retail electricity in NT or WA, so this should not affect
anyone. If it affects you, please open an issue. If you would rather have
correct hourly timing than older history, the only manual remedy is a
full rebuild:

1. Delete the `haggle:*` rows from the recorder's `statistics` and
   `statistics_short_term` tables (the procedure the CHANGELOG uses for
   earlier recoveries).
2. Let the 30-day backfill rebuild them at the corrected hours. This takes
   about 5 daily polls.

**That permanently loses everything older than 30 days.** Do this only if
you accept the loss.

**Recent days are missing.**
Expected — 24–48 h feed lag. If a day is still missing after 3 days, check
`home-assistant.log` for `custom_components.haggle` warnings and open an
issue.
