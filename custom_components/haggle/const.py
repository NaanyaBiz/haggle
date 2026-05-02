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

# Polling cadences.
# AGL interval data is delayed 24-48 h from the meter (AEMO feed lag).
SCAN_INTERVAL_HOURLY: Final = timedelta(hours=24)  # 30-min intervals: fetch yesterday
SCAN_INTERVAL_DAILY: Final = timedelta(hours=6)  # daily series: pick up new days
SCAN_INTERVAL_PLAN: Final = timedelta(days=7)  # plan/rates: rarely changes

# How many seconds before access-token expiry to proactively refresh.
# AGL access tokens expire at ~15 min; refresh 2 min early.
TOKEN_REFRESH_MARGIN_SECONDS: Final = 120

# Number of days of history to backfill on first install.
BACKFILL_DAYS: Final = 30
# Maximum days to fetch per 24 h poll cycle (throttles first-install backfill).
BACKFILL_CHUNK_DAYS: Final = 7

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

# Config-entry keys.
# CONF_EMAIL / CONF_PASSWORD are NOT used — auth is via refresh token.
CONF_REFRESH_TOKEN: Final = "refresh_token"  # ← it IS a token key
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_ACCESS_TOKEN_EXPIRY: Final = "access_token_expiry"
CONF_CONTRACT_NUMBER: Final = "contract_number"
CONF_ACCOUNT_NUMBER: Final = "account_number"

# Coordinator data attribute names — must match HaggleData field names exactly.
DATA_CONSUMPTION_KWH: Final = "latest_cumulative_kwh"  # TOTAL_INCREASING sensor
DATA_CONSUMPTION_PERIOD: Final = "consumption_period_kwh"  # kWh this bill period
DATA_CONSUMPTION_COST: Final = "consumption_period_cost_aud"  # cumulative AUD cost
DATA_BILL_PROJECTION: Final = "bill_projection_aud"  # AUD forecast for current period
DATA_UNIT_RATE: Final = "unit_rate_aud_per_kwh"  # AUD/kWh
DATA_SUPPLY_CHARGE: Final = "supply_charge_aud_per_day"  # AUD/day
