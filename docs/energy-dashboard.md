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
| **Consumption** | Cumulative kWh ever imported by the integration. Mirrors the statistics total; moves once per daily poll. |
| **Consumption this period** | kWh so far in the current AGL billing period (matches the app's "Usage So Far"). |
| **Consumption cost** | AUD so far in the current billing period. |
| **Bill projection** | AGL's own forecast for the current bill. |
| **Unit rate / Supply charge** | Your plan's c/kWh (as AUD/kWh) and daily supply charge. |
| **Unit rate (peak / off-peak / shoulder)** | Per-band rates — ToU contracts only. |
| **Solar generation / Solar feed-in credit** | Cumulative exported kWh / credited AUD ever imported — solar contracts only. Like **Consumption**, these are running totals: do not compare them to the app's billing-period tile. |
| **Solar sold this period / Solar feed-in credit this period** | Exported kWh / credited AUD in the current billing period — these are the numbers that match the AGL app's "Sold To Grid" tile. Caveat: on **quarterly billing**, a period that started more than 30 days ago predates the backfill window, so these cover only the stored 30-day history and will read lower than the app until a new period starts. |
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
the statistic already holds the hourly history. (Reported as
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

**Recent days are missing.**
Expected — 24–48 h feed lag. If a day is still missing after 3 days, check
`home-assistant.log` for `custom_components.haggle` warnings and open an
issue.
