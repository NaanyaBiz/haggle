"""Constants for the haggle integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "haggle"

# Auth0 / AGL API
AGL_AUTH_HOST: Final = "https://secure.agl.com.au"
AGL_API_HOST: Final = "https://api.platform.agl.com.au"
# iOS app client_id — matches AGL mobile app 8.38.0-531 (documented 2026-04-30).
AGL_CLIENT_ID: Final = "2mDkNcC8gkDLL7FTT1ZxF5rrQHrLTHL3"
# Match the AGL mobile app client-flavor; AGL servers may reject unknown clients.
AGL_CLIENT_FLAVOR: Final = "app.iOS.public.8.38.0-531"
AGL_USER_AGENT: Final = "AGL/531 CFNetwork/3860.500.112 Darwin/25.4.0"

# Auth0 PKCE / OAuth2 redirect
AGL_AUTH0_CLIENT: Final = (
    "eyJuYW1lIjoiQXV0aDAuc3dpZnQiLCJ2ZXJzaW9uIjoiMi4xMi4wIiwiZW52Ijp7ImlPUyI6IjI2"
    "LjQiLCJzd2lmdCI6IjYueCJ9fQ"
)
AGL_REDIRECT_URI: Final = "https://secure.agl.com.au/ios/au.com.agl.mobile/callback"
AGL_OAUTH_SCOPE: Final = "openid profile email offline_access"
AGL_OAUTH_AUDIENCE: Final = "https://api.platform.agl.com.au/"

# Polling cadence.
# AGL interval data is delayed 24-48 h from the meter (AEMO feed lag).
SCAN_INTERVAL_HOURLY: Final = timedelta(hours=24)  # 30-min intervals: fetch yesterday
# Retry cadence after a FAILED poll (#155). A transient AGL error at poll time
# previously cost a full 24 h of data — indistinguishable from "the poll never
# ran" (#126). Restored to SCAN_INTERVAL_HOURLY on the next success; polling
# faster than 24 h on success buys nothing (AGL data lags 24-48 h).
RETRY_INTERVAL_ON_ERROR: Final = timedelta(minutes=30)
# Normal-path backfill give-up (#154): after this many consecutive cycles in
# which the SAME solar chunk made zero progress (every attempted day errored,
# no 429 involved), write zero-delta marker rows past the span so the resume
# point advances — mirroring the accepted rare-hole tradeoff for single days.
# In-memory counter; an HA restart resets it (conservative: more retries,
# never fewer).
SOLAR_STALL_GIVE_UP_CYCLES: Final = 3

# Number of days of history to backfill on first install.
BACKFILL_DAYS: Final = 30
# Maximum days to fetch per 24 h poll cycle (throttles first-install backfill).
BACKFILL_CHUNK_DAYS: Final = 7
# Seconds to sleep between per-day fetches in a backfill chunk so we don't
# fire 7 requests in under a second.
BACKFILL_INTER_REQUEST_DELAY: Final = 0.5
# Trailing days to re-fetch every poll once initial backfill is complete.
# Self-heals AGL's day-late AEMO backfills: a slot first returned as a
# placeholder gets overwritten once AGL has the real read.
# async_add_external_statistics is idempotent on (statistic_id, start), so
# this is a safe overwrite.
REWINDOW_DAYS: Final = 7
# Max seconds to wait for the recorder to commit queued statistics after a
# COMPLETE heal sweep before reading the bill-period baseline (#152). On
# timeout the period sensors stay `unknown` for the cycle (safe fallback).
RECORDER_DRAIN_TIMEOUT: Final = 30.0

# AGL BFF requires these headers on Hourly/Daily usage endpoints (HTTP 500 without them).
# Documented from AGL mobile app 8.38.0-531 — 2026-05-01.
AGL_ACCEPT_FEATURES: Final = (
    "AccountEnableCarbonNeutral, AccountEnableCarbonNeutralMessagingRemoval,"
    " AccountEnableConcessionMessaging, AccountEnableConsumerDataRight,"
    " AccountEnableDirectDebitSetup, AccountEnableHideTelcoNoChangeWarning,"
    " AccountEnableMessagingInfoItem, AccountEnableNativeAccountDeletion,"
    " AccountEnableTelcoDirectDebit, BillingEnableDirectDebitSetup,"
    " BillingEnableEnergyPaymentDirectDebit, BillingEnableEnergyViewPlan,"
    " BillingEnablePaymentArrangement, BillingEnableTransactionHistory,"
    " BillingEnableUpdatedPastBillsFlow, BillingEnableV3,"
    " DeeplinkEnableBpFuelOffer, DeeplinkEnableBpPulseOffer,"
    " DeeplinkEnableElectrifyNowLanding, DeeplinkEnableInAppSales,"
    " DeeplinkEnablePushNotificationPreferences, DeeplinkEnableTransactionHistory,"
    " ElectricityServiceHubEnableFinancialHardship, ElectricityServiceHubEnableMessaging,"
    " EnableAglAssistantRebrand, EnableHighBillProjectionTreatment,"
    " EnableInAppSales, EnableMobileSimActivationSetting, EnableServiceHub,"
    " EnableTelcoServiceHub, EnableUsageFromOverview, EnergyPlanEnableManageActions,"
    " EnergyServiceHubEnableBudgetTracker, EnergyServiceHubEnableElectricityUsageDisclaimer,"
    " EnergyServiceHubEnableHidingSettingsTitle, HelpCentreEnableArrangeYourMoveQuickLink,"
    " HelpCentreEnableDisconnectMessagingFaq, HelpCentreEnableSetupTwoFactorAuthentication,"
    " HelpEnableConsumerDataRight, HelpEnableFamilyDomesticViolenceSupport,"
    " HelpEnableTelcoFinancialHardshipLink, InAppSalesEnableElectrifyNow,"
    " InAppSalesEnablePeakEnergyRewards, InAppSalesEnableRewardsTile,"
    " InAppSalesEnableTelcoOffersSourceChange, LoginEnableAuth0HttpsCallbacks,"
    " LoginEnablePasskey, LoginEnablePasskeyButton, MessagingEnableUpdateSdk,"
    " OffersEnableOverviewAlertBanner, OffersEnableV3,"
    " OverviewAndAccountEnableSecurityCentre, OverviewAndViewPlanEnableNetflix,"
    " OverviewEnableHideTelcoCarbonNeutralLabel, OverviewEnableMessaging,"
    " OverviewEnableMultiOffers, OverviewEnableOffer, OverviewEnableV3,"
    " OverviewV3EnableSolarHealth, PushEnablePreferenceManagement,"
    " PushPreferencesEnableHasTelcoFlag, QuickTourEnable,"
    " ServiceHubEnableAccessVirtualCircuitId, ServiceHubEnableEnergyPlan,"
    " ServiceHubEnableMobileChangePlan, ServiceHubEnableMobileConfiguration,"
    " ServiceHubEnableNbnChangePlan, ServiceHubEnableNbnCostOfPlanDisclaimer,"
    " ServiceHubEnableUsageInsightSmartElectricity,"
    " ServiceHubEnableUsageInsightSmartElectricitySolar,"
    " TelcoServiceHubEnableMobileESimCopy, UsageEnableBattery,"
    " UsageEnableHistoricalMeterReads, UsageEnableMultiMeterRead,"
    " UsageEnableVirtualPowerPlant, UsageInsightEnableSolarRecommendation,"
    " VirtualPowerPlantEnableByobV3"
)
AGL_CLIENT_DEVICE: Final = "Apple-iPhone-iPhone14,7-iOS-26.4.2"  # documented 2026-05-01
# Screen scaling vector required by the BFF for usage chart rendering.
AGL_SCALING: Final = "36.514404_108.057_40.670903_120.357_0_0_0_0"

# Statistic ID suffixes — full ID is f"{DOMAIN}:{STAT_*}_{contract_number}"
STAT_CONSUMPTION: Final = "consumption"  # → haggle:consumption_{contract}
STAT_COST: Final = "cost"  # → haggle:cost_{contract}
# Solar feed-in (export) series — written only for contracts with hasSolar.
# The generation series is a "Return to grid" source in the Energy dashboard.
STAT_GENERATION: Final = "generation"  # → haggle:generation_{contract}
STAT_GENERATION_CREDIT: Final = (
    "generation_credit"  # → haggle:generation_credit_{contract}
)

# Time-of-Use tariff types — the value of `consumption.type` on each interval.
TARIFF_PEAK: Final = "peak"
TARIFF_OFFPEAK: Final = "offpeak"
TARIFF_SHOULDER: Final = "shoulder"
TARIFF_NORMAL: Final = "normal"
# Presence of ANY of these in interval data marks the contract as Time-of-Use.
TOU_BANDS: Final = (TARIFF_PEAK, TARIFF_OFFPEAK, TARIFF_SHOULDER)
# On a ToU contract, every tariff type that appears gets its own statistic
# series (incl. `normal`/anytime) so the per-tariff series sum back to the
# aggregate with no lost kWh. Per-tariff statistic IDs are
# f"{DOMAIN}:{STAT_CONSUMPTION}_{tariff}_{contract}" / "..._{STAT_COST}_...".
TOU_SERIES_TARIFFS: Final = (
    TARIFF_PEAK,
    TARIFF_OFFPEAK,
    TARIFF_SHOULDER,
    TARIFF_NORMAL,
)
# Stable, band-distinct labels embedded in StatisticMetaData.name so the
# Energy dashboard picker can tell the per-tariff series apart. MUST be stable
# across calls — the recorder updates metadata in place on every import.
TARIFF_LABELS: Final = {
    TARIFF_PEAK: "Peak",
    TARIFF_OFFPEAK: "Off-Peak",
    TARIFF_SHOULDER: "Shoulder",
    TARIFF_NORMAL: "Anytime",
}

# Config-entry keys.
# CONF_EMAIL / CONF_PASSWORD are NOT used — auth is via refresh token.
CONF_REFRESH_TOKEN: Final = "refresh_token"  # ← it IS a token key
CONF_CONTRACT_NUMBER: Final = "contract_number"
CONF_ACCOUNT_NUMBER: Final = "account_number"
# SHA-256 hex of the leaf-cert SPKI captured at config-flow time. Empty string
# = no pin yet (older entries pre-PR4 / capture failed at install time).
CONF_PINNED_SPKI_AUTH: Final = "pinned_spki_auth"  # secure.agl.com.au
CONF_PINNED_SPKI_BFF: Final = "pinned_spki_bff"  # api.platform.agl.com.au
# One-time solar generation leading-hole heal (#128). Stored as a record:
#   {"state": "pending"|"done", "floor": "YYYY-MM-DD", "attempts": int}
# Absent = never attempted. "pending" = a full-window re-import is in progress;
# the `floor` is frozen when the heal starts so a 429-interrupted retry re-fetches
# the SAME window instead of sliding forward and dropping its oldest day (Codex
# P2 on #150). "done" = the heal has run and must never re-arm (so an unfetchable
# permanent leading gap is not re-swept every poll). A sweep that leaves any day
# skipped (429 or transient AGL 5xx) stays pending and retries, up to
# MAX_SOLAR_HEAL_ATTEMPTS sweeps, then gives up to "done" so a permanently-erroring
# old day can't wedge the heal forever (matches _fetch_day_solar's accepted
# rare-hole tradeoff).
CONF_SOLAR_HEAL: Final = "solar_heal"
SOLAR_HEAL_PENDING: Final = "pending"
SOLAR_HEAL_DONE: Final = "done"
MAX_SOLAR_HEAL_ATTEMPTS: Final = 3

# Coordinator data attribute names — must match HaggleData field names exactly.
DATA_CONSUMPTION_KWH: Final = "latest_cumulative_kwh"  # TOTAL_INCREASING sensor
DATA_CONSUMPTION_PERIOD: Final = "consumption_period_kwh"  # kWh this bill period
DATA_CONSUMPTION_COST: Final = "consumption_period_cost_aud"  # cumulative AUD cost
DATA_BILL_PROJECTION: Final = "bill_projection_aud"  # AUD forecast for current period
DATA_UNIT_RATE: Final = "unit_rate_aud_per_kwh"  # AUD/kWh
DATA_SUPPLY_CHARGE: Final = "supply_charge_aud_per_day"  # AUD/day
# Per-tariff unit rates (AUD/kWh) — only populated/registered on ToU contracts.
DATA_UNIT_RATE_PEAK: Final = "unit_rate_peak_aud_per_kwh"
DATA_UNIT_RATE_OFFPEAK: Final = "unit_rate_offpeak_aud_per_kwh"
DATA_UNIT_RATE_SHOULDER: Final = "unit_rate_shoulder_aud_per_kwh"
# Solar feed-in cumulatives — only populated/registered on hasSolar contracts.
DATA_GENERATION_KWH: Final = "latest_generation_kwh"  # TOTAL_INCREASING sensor
DATA_GENERATION_CREDIT: Final = "latest_generation_credit_aud"  # cumulative AUD
# Bill-period solar totals (match the app's "Sold To Grid" tile) + feed-in rate.
DATA_GENERATION_PERIOD: Final = "generation_period_kwh"  # kWh exported this period
DATA_GENERATION_PERIOD_CREDIT: Final = "generation_period_credit_aud"  # AUD credited
DATA_FEED_IN_RATE: Final = "feed_in_rate_aud_per_kwh"  # AUD/kWh
