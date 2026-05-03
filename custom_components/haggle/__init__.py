"""The haggle integration.

Fetches smart-meter data from the AGL Energy API and feeds it into the
HA Energy dashboard. See AGENTS.md for design notes (Auth0 refresh-token
rotation, daily polling, import_statistics for historical data).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.const import Platform

from .agl.client import AglAuth, AglClient
from .agl.pinning import AGL_AUTH_HOST_NAME
from .const import (
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


async def async_setup_entry(hass: HomeAssistant, entry: HaggleConfigEntry) -> bool:
    """Set up haggle from a config entry."""
    refresh_token = entry.data[CONF_REFRESH_TOKEN]
    contract_number: str = entry.data.get(CONF_CONTRACT_NUMBER, "")
    pinned_auth: str = entry.data.get(CONF_PINNED_SPKI_AUTH, "")
    pinned_bff: str = entry.data.get(CONF_PINNED_SPKI_BFF, "")

    _LOGGER.info(
        "Setting up haggle entry: contract=%s pin_auth=%s pin_bff=%s",
        contract_number or "unknown",
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

    # Pin-check is fire-and-forget per request: compare observed SPKI against
    # the TOFU value captured at config-flow time. Mismatch surfaces as a HA
    # persistent notification + WARNING log, but does NOT block the request —
    # legitimate AGL cert rotations should not brick HACS users. Re-pin via
    # the standard Reconfigure flow.
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

    session = aiohttp.ClientSession()
    auth = AglAuth(refresh_token, _persist_refresh_token, pin_check=_check_pin)
    client = AglClient(auth, session, pin_check=_check_pin)
    coordinator = HaggleCoordinator(hass, entry, client, contract_number)  # type: ignore[arg-type]

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = HaggleRuntimeData(
        auth=auth,
        client=client,
        coordinator=coordinator,
        session=session,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HaggleConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.session.close()
    return unload_ok
