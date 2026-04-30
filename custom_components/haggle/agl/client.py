"""AGL Neighbourhood portal client.

Stub. Real implementation lands in Sprint 1; see AGENTS.md > AGL gotchas.

Auth shape: email + password -> OTP step -> persistent session cookie.
The cookie is stored on the config entry; when it expires, the
coordinator surfaces `AGLAuthError` -> HA reauth flow re-prompts for OTP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


class AGLError(Exception):
    """Base class for AGL client errors."""


class AGLAuthError(AGLError):
    """Session expired or credentials invalid; reauth required."""


class AGLClient:
    """Async client for the AGL Neighbourhood customer portal."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        """Initialize the client."""
        self._hass = hass
        self._entry = entry

    @classmethod
    def from_entry(cls, hass: HomeAssistant, entry: ConfigEntry[Any]) -> AGLClient:
        """Build a client from a config entry."""
        return cls(hass, entry)

    async def async_ensure_session(self) -> None:
        """Ensure a valid portal session exists; refresh if cookie present."""
        raise NotImplementedError("agl-portal-explorer: implement in Sprint 1")

    async def async_login(self, email: str, password: str) -> None:
        """Submit credentials; AGL responds by sending an OTP."""
        raise NotImplementedError("agl-portal-explorer: implement in Sprint 1")

    async def async_submit_otp(self, otp: str) -> None:
        """Submit the OTP; success persists a session cookie on the entry."""
        raise NotImplementedError("agl-portal-explorer: implement in Sprint 1")

    async def async_list_nmis(self) -> list[str]:
        """Return the NMIs visible to the authenticated account."""
        raise NotImplementedError("agl-portal-explorer: implement in Sprint 1")

    async def async_fetch_usage(self) -> dict[str, Any]:
        """Fetch the latest NEM12 export and return parsed totals.

        Returns a dict with keys from `const.DATA_*`.
        """
        raise NotImplementedError("agl-portal-explorer: implement in Sprint 1")

    async def async_close(self) -> None:
        """Release any held HTTP resources."""
        # No-op for the stub.
