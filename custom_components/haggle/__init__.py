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

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .agl.client import AglAuth, AglClient
from .agl.pinning import AGL_AUTH_HOST_NAME, HagglePinningConnector
from .const import (
    AGL_AUTH0_CLIENT,
    AGL_AUTH_HOST,
    AGL_CLIENT_FLAVOR,
    AGL_CLIENT_ID,
    AGL_USER_AGENT,
    CONF_CONTRACT_NUMBER,
    CONF_PINNED_SPKI_AUTH,
    CONF_PINNED_SPKI_BFF,
    CONF_REFRESH_TOKEN,
)
from .coordinator import HaggleCoordinator

if TYPE_CHECKING:
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
    # AGL cert rotations should not brick HACS users. Re-pin via Reconfigure.
    def _check_pin(host: str, observed: str) -> None:
        expected = pinned_auth if host == AGL_AUTH_HOST_NAME else pinned_bff
        if not expected or observed == expected:
            return
        _LOGGER.warning(
            "Pinned SPKI mismatch for %s (stored=%s observed=%s) — investigate or reauth",
            host,
            expected[:12],
            observed[:12],
        )
        persistent_notification.async_create(
            hass,
            title="haggle: AGL certificate changed",
            message=(
                f"The TLS certificate for {host} no longer matches the value "
                "captured during initial setup. If you are on a trusted network, "
                "click Reconfigure on the haggle integration to re-pin. If this "
                "is unexpected, suspect a man-in-the-middle on your local network."
            ),
            notification_id=f"haggle_pin_mismatch_{host}",
        )

    connector = HagglePinningConnector(on_new_connection=_check_pin)
    session = aiohttp.ClientSession(connector=connector)
    auth = AglAuth(refresh_token, _persist_refresh_token)
    client = AglClient(auth, session)
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
    """Unload a config entry — close the owned aiohttp session on the way out."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.session.close()
    return unload_ok


async def _async_revoke_grant(hass: HomeAssistant, entry: HaggleConfigEntry) -> None:
    """Best-effort server-side revocation of the stored refresh token (CO-11.4).

    HA deletes entry.data with the entry, but the Auth0 grant would otherwise
    stay valid server-side until idle expiry. Auth0's /oauth/revoke accepts
    public-client (no secret) revocation and, with rotation enabled, revokes
    the whole token family. Every failure is swallowed: removal must never be
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
    await _async_revoke_grant(hass, entry)
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    for entity in entries:
        registry.async_remove(entity.entity_id)
