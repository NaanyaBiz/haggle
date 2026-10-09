"""The haggle integration.

Fetches smart-meter data from the AGL Energy API and feeds it into the
HA Energy dashboard. See AGENTS.md for design notes (Auth0 refresh-token
rotation, daily polling, import_statistics for historical data).

The integration owns its own `aiohttp.ClientSession` (rather than using
HA's shared `async_get_clientsession`) because the TOFU TLS pinning needs
a custom `TCPConnector` subclass — `HagglePinningConnector` — that captures
the leaf-cert SPKI for every new connection. HA's shared session uses HA's
own connector which we cannot subclass. The session is closed in
`async_unload_entry`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .agl.client import AglAuth, AglClient
from .agl.parser import tz_for_address
from .agl.pinning import AGL_AUTH_HOST_NAME, HagglePinningConnector
from .const import (
    AGL_API_TZ_KEY,
    AGL_AUTH0_CLIENT,
    AGL_AUTH_HOST,
    AGL_CLIENT_FLAVOR,
    AGL_CLIENT_ID,
    AGL_USER_AGENT,
    CONF_CONTRACT_NUMBER,
    CONF_LOCAL_TZ,
    CONF_PINNED_SPKI_AUTH,
    CONF_PINNED_SPKI_BFF,
    CONF_REFRESH_TOKEN,
    PIN_MISMATCH_NOTIFICATION_ID,
)
from .coordinator import HaggleCoordinator

if TYPE_CHECKING:
    from datetime import tzinfo

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Bounded so a hung AGL endpoint cannot stall entry removal.
_REVOKE_TIMEOUT = aiohttp.ClientTimeout(total=10)

type HaggleConfigEntry = ConfigEntry[HaggleRuntimeData]


@dataclass(slots=True)
class HaggleRuntimeData:
    """Per-config-entry runtime state.

    Stored on `entry.runtime_data` (HA 2025.1+ pattern).
    """

    auth: AglAuth
    client: AglClient
    coordinator: HaggleCoordinator
    session: aiohttp.ClientSession
    connector: HagglePinningConnector


async def async_migrate_entry(hass: HomeAssistant, entry: HaggleConfigEntry) -> bool:
    """Migrate a config entry to the current schema (currently 1.2).

    1.1 → 1.2 (#268, #292): persist the contract's timezone as
    CONF_LOCAL_TZ. Both creation paths store the contract's service address
    as the entry title, so the zone is derived from it here; a title the
    user renamed (or the legacy `AGL <contract>` fallback) yields "" and the
    entry runs on the HA-timezone fallback — window only, no Sydney-conversion
    correction — until the first successful overview cycle persists the
    address-derived zone (coordinator._refresh_from_overview) or the user
    runs Reconfigure. A failed derivation is "", never a refused load.

    A key already stored on a 1.1 entry (a repair flow that wrote it before
    the reload reached this migration) is kept — the title-derived value is
    only ever a fallback for an entry that has none.

    Only a minor bump: HA refuses to load an entry whose MAJOR version is
    newer than the handler's, so a major bump would break the downgrade
    path README.md promises; an older release loads a 1.2 entry unchanged.
    """
    if entry.version == 1 and entry.minor_version < 2:
        key: str = entry.data.get(CONF_LOCAL_TZ, "")
        if not key:
            # ZoneInfo(key) inside tz_for_address reads tzdata on a cache
            # miss — keep that off the event loop.
            tz = await hass.async_add_executor_job(tz_for_address, entry.title or "")
            key = getattr(tz, "key", "") if tz is not None else ""
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_LOCAL_TZ: key}, minor_version=2
        )
        # The title is the service address (Class B-adjacent) — log the
        # derived zone, never the title.
        _LOGGER.info(
            "Migrated haggle entry to 1.2: local_tz=%s", key or "unknown (HA tz)"
        )
    return True


def _resolve_local_tz(entry: HaggleConfigEntry) -> tuple[tzinfo, bool]:
    """(zone, zone is the contract's own) for the AglClient.

    Resolution order: ZoneInfo(entry.data[CONF_LOCAL_TZ]) → HA's configured
    timezone. The stored key is address-derived (config flow, migration,
    repair flows, overview refinement) and is the only source the parser
    may undo AGL's Sydney conversion against (#292, A3); the HA fallback
    bounds the interval window only. A hand-edited key that tzdata cannot
    resolve degrades to the fallback rather than failing setup — the client
    itself announces the fallback once, so only the reason is logged here.
    """
    key: str = entry.data.get(CONF_LOCAL_TZ, "")
    if key:
        try:
            return ZoneInfo(key), True
        except KeyError, OSError, ValueError:
            # The key is user-editable text; log it bounded.
            _LOGGER.warning(
                "Stored local_tz %.64r is not a usable timezone key; "
                "run Reconfigure to re-derive it from the service address",
                key,
            )
    return dt_util.get_default_time_zone(), False


async def async_setup_entry(hass: HomeAssistant, entry: HaggleConfigEntry) -> bool:
    """Set up haggle from a config entry."""
    refresh_token = entry.data[CONF_REFRESH_TOKEN]
    contract_number: str = entry.data.get(CONF_CONTRACT_NUMBER, "")
    pinned_auth: str = entry.data.get(CONF_PINNED_SPKI_AUTH, "")
    pinned_bff: str = entry.data.get(CONF_PINNED_SPKI_BFF, "")

    _LOGGER.info(
        "Setting up haggle entry: contract=%s pin_auth=%s pin_bff=%s",
        # Class B identifier (docs/threat-model.md §2): log last-4 only.
        f"…{contract_number[-4:]}" if contract_number else "unknown",
        "set" if pinned_auth else "unset",
        "set" if pinned_bff else "unset",
    )

    async def _persist_refresh_token(new_token: str) -> None:
        """Persist rotated refresh token back to config entry data.

        Auth0 has already consumed the previous refresh token by the time this
        callback runs; failing to persist the new one means the next HA restart
        will load a stale (revoked) token. Surface that immediately via the
        reauth flow rather than letting the user discover it on next restart.
        """
        try:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: new_token}
            )
            _LOGGER.debug("Refresh token persisted (len=%d)", len(new_token))
        except Exception:
            _LOGGER.exception(
                "Failed to persist rotated refresh token — triggering reauth"
            )
            entry.async_start_reauth(hass)

    # Pin-check fires synchronously when HagglePinningConnector creates a new
    # connection (after the TLS handshake completes). Mismatch surfaces as a
    # HA persistent notification + WARNING log, but does NOT raise — legitimate
    # AGL cert rotations should not brick HACS users. Re-pin via Reconfigure
    # (config_flow.async_step_reconfigure) — reauth deliberately never
    # overwrites a stored pin (config_flow._pin_updates).
    #
    # Each distinct mismatching fingerprint is reported ONCE per entry setup
    # (#280): the connector opens a new TLS connection per poll (and more
    # under retries), so without this a single AGL rotation logged a WARNING
    # on every connection for as long as the user had not re-pinned. The
    # persistent notification is not re-created on repeats either (a
    # dismissed notice stays dismissed until re-pin/reload/restart —
    # accepted, #280); a *different* mismatch (another cert) is still
    # reported. A reload/restart resets the memory.
    reported_mismatches: set[tuple[str, str]] = set()

    def _check_pin(host: str, observed: str) -> None:
        # Read the pin LIVE from entry.data, not from the setup-time locals:
        # Reconfigure writes the new pins and dismisses the notice BEFORE the
        # scheduled reload unloads this instance, and a connection opened in
        # that window must compare against the new pin or the notice the user
        # just cleared comes straight back (#275).
        expected: str = entry.data.get(
            CONF_PINNED_SPKI_AUTH
            if host == AGL_AUTH_HOST_NAME
            else CONF_PINNED_SPKI_BFF,
            "",
        )
        if not expected or observed == expected:
            return
        if (host, observed) in reported_mismatches:
            _LOGGER.debug(
                "Pinned SPKI mismatch for %s unchanged (observed=%s) — already reported",
                host,
                observed[:12],
            )
            return
        reported_mismatches.add((host, observed))
        _LOGGER.warning(
            "Pinned SPKI mismatch for %s (stored=%s observed=%s) — if AGL rotated "
            "its certificate, run Reconfigure on the Haggle entry to re-pin; "
            "otherwise suspect TLS interception",
            host,
            expected[:12],
            observed[:12],
        )
        persistent_notification.async_create(
            hass,
            title="haggle: AGL certificate changed",
            message=(
                f"The TLS certificate key for {host} no longer matches the "
                "fingerprint Haggle pinned for it. Requests keep working; this is "
                "a warning only. AGL replaces its certificates from time to time: "
                "if you are on a network you trust, open Settings → Devices & "
                "services → AGL Haggle, choose Reconfigure from the entry's ⋮ menu "
                "and log in again to re-pin (repeat for each Haggle entry). If "
                "you did not expect this, or your network inspects TLS traffic "
                "(corporate proxy, security appliance), do NOT re-pin: re-pinning "
                "on an intercepted network makes the interceptor's certificate "
                "the trusted one."
            ),
            notification_id=PIN_MISMATCH_NOTIFICATION_ID.format(host=host),
        )

    connector = HagglePinningConnector(on_new_connection=_check_pin)
    session = aiohttp.ClientSession(connector=connector)
    # Bind the session's lifetime to the entry the moment it exists (#247).
    # This integration owns its session (HagglePinningConnector cannot run
    # under HA's shared connector), and `entry.runtime_data` — assigned
    # below — used to be the only handle that could reach it. But the
    # mandatory first refresh routinely raises ConfigEntryNotReady or
    # ConfigEntryAuthFailed on any transient AGL/network error, and HA then
    # retries setup with backoff: every attempt stranded another session +
    # connector, open until aiohttp's finalizer eventually noticed.
    #
    # `async_on_unload` rather than a try/except because HA runs these
    # callbacks from `_async_setup_entry`'s own finally block on EVERY
    # failure path — including `asyncio.CancelledError` (a BaseException, so
    # a bare `except Exception` would have missed a cancelled setup, e.g. on
    # shutdown mid-retry) — and again on a successful unload. One
    # registration, every path, no double bookkeeping.
    entry.async_on_unload(session.close)

    auth = AglAuth(refresh_token, _persist_refresh_token)
    # The contract's local tz bounds the interval-timestamp window to the
    # true UTC shape of one local day (Codex P1, PR #266) and, when it is
    # address-derived, is what the parser re-localises AGL's
    # Sydney-converted timestamps into (#292). HA's configured tz stands in
    # for the window only until the zone is known.
    # Both zone loads are a tzdata file read on a cache miss — a contract
    # zone that differs from HA's own is exactly the #292 case — so they
    # run off the event loop. The second call only pre-warms the
    # process-wide ZoneInfo cache with AGL's conversion zone, so the
    # parser's first `_zone_for_key(AGL_API_TZ_KEY)` is a cache hit; a
    # host without that tzdata entry returns None here and the parser's
    # own `_default_api_tz` warns about it.
    local_tz, tz_is_contract = await hass.async_add_executor_job(
        _resolve_local_tz, entry
    )
    await dt_util.async_get_time_zone(AGL_API_TZ_KEY)
    client = AglClient(auth, session, local_tz=local_tz, tz_is_contract=tz_is_contract)
    coordinator = HaggleCoordinator(hass, entry, client, contract_number)  # type: ignore[arg-type]

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = HaggleRuntimeData(
        auth=auth,
        client=client,
        coordinator=coordinator,
        session=session,
        connector=connector,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HaggleConfigEntry) -> bool:
    """Unload a config entry.

    The owned aiohttp session is closed by the `async_on_unload` callback
    registered in `async_setup_entry` (#247) — HA runs it after a successful
    unload, and on every failed-setup path too. Closing it here as well
    would be redundant (`ClientSession.close()` is idempotent, but the
    single registration is the clearer contract).
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_revoke_grant(hass: HomeAssistant, entry: HaggleConfigEntry) -> None:
    """Best-effort server-side revocation of the stored refresh token (CO-11.4).

    HA deletes entry.data with the entry, but the Auth0 grant would otherwise
    stay valid server-side until idle expiry. Auth0's /oauth/revoke accepts
    public-client (no secret) revocation and, with rotation enabled, revokes
    the whole token family — which is also why revoking a possibly-STALE
    token is fine: if the last rotation's persist failed (reauth path), the
    entry holds the consumed predecessor, and revoking any family member
    still invalidates the entire grant. Every failure is swallowed: removal must never be
    blocked by AGL/network state, and a failed revoke leaves the user exactly
    where they are today (README documents the AGL-side fallback).

    Uses HA's shared session: the integration-owned pinned session is already
    closed by unload, and TOFU pinning is warn-only (never blocks), so no
    protection is lost — CA validation still applies.
    """
    token: str = entry.data.get(CONF_REFRESH_TOKEN, "")
    if not token:
        return
    try:
        resp = await async_get_clientsession(hass).post(
            f"{AGL_AUTH_HOST}/oauth/revoke",
            json={"client_id": AGL_CLIENT_ID, "token": token},
            headers={
                "Client-Flavor": AGL_CLIENT_FLAVOR,
                "auth0-client": AGL_AUTH0_CLIENT,
                "User-Agent": AGL_USER_AGENT,
            },
            timeout=_REVOKE_TIMEOUT,
        )
        async with resp:
            if resp.ok:
                _LOGGER.info("AGL sign-in grant revoked at Auth0 on removal")
            else:
                # Body deliberately not read — raw Auth0 bodies never reach logs.
                _LOGGER.warning(
                    "Auth0 revoke returned HTTP %s on removal (ignored — "
                    "best-effort; revoke via AGL account settings if needed)",
                    resp.status,
                )
    except Exception:  # best-effort by design; removal must proceed
        _LOGGER.warning(
            "Auth0 revoke failed on removal (ignored — best-effort; revoke via "
            "AGL account settings if needed)"
        )


async def async_remove_entry(hass: HomeAssistant, entry: HaggleConfigEntry) -> None:
    """Drop entity-registry rows for this entry on integration removal.

    Also best-effort revokes the Auth0 refresh-token grant server-side — the
    only user data this integration controls that would otherwise outlive
    uninstall (CO-11.4).

    Without this, deleting the integration leaves orphan rows whose
    `config_entry_id` references the now-gone entry. On reinstall, HA
    sees an entity_id collision and renames the new sensors with a `_2`
    suffix; the orphans then linger forever as `unavailable`.

    Deliberately does NOT clear the `haggle:*` external statistics from the
    recorder (#91). Those rows are the user's own historical energy/cost data;
    silently destroying years of Energy-dashboard history on an uninstall would
    be surprising and unrecoverable. Orphaned statistics are harmless and the
    user can prune them on their own terms via
    Developer Tools → Statistics → "Fix issue" (orphaned statistics), so the
    non-destructive default is the right one. Do not add async_clear_statistics
    here without an explicit opt-in.
    """
    # Local cleanup FIRST: a slow/blackholed Auth0 endpoint (or a shutdown
    # cancelling the await below) must never leave orphan registry rows —
    # that is the exact bug this function exists to prevent.
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    for entity in entries:
        registry.async_remove(entity.entity_id)
    await _async_revoke_grant(hass, entry)
