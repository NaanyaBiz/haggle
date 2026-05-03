"""Smoke tests for setup / unload roundtrip."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haggle.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.haggle.coordinator import HaggleData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_ENTRY_DATA = {
    CONF_REFRESH_TOKEN: "v1.testtoken",
    CONF_CONTRACT_NUMBER: "9999999999",
    CONF_ACCOUNT_NUMBER: "1234567890",
}

_COORDINATOR_DATA = HaggleData(
    consumption_period_kwh=200.0,
    consumption_period_cost_aud=45.00,
    bill_projection_aud=90.00,
    unit_rate_aud_per_kwh=0.33792,
    supply_charge_aud_per_day=1.31714,
    latest_cumulative_kwh=200.0,
)


async def test_setup_and_unload(hass: HomeAssistant) -> None:
    """Config entry sets up, registers sensor platform, then unloads cleanly."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_ENTRY_DATA,
        unique_id="1234567890_9999999999",
    )
    entry.add_to_hass(hass)

    mock_session = MagicMock()
    mock_session.close = AsyncMock()

    with (
        patch(
            "custom_components.haggle.aiohttp.ClientSession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.haggle.agl.client.AglAuth.async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="access_token",
        ),
        patch(
            "custom_components.haggle.coordinator.HaggleCoordinator._async_setup",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.haggle.coordinator.HaggleCoordinator._async_update_data",
            new_callable=AsyncMock,
            return_value=_COORDINATOR_DATA,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.runtime_data is not None
        assert entry.runtime_data.coordinator.last_update_success is True

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        # The integration owns its session (HagglePinningConnector cannot run
        # under HA's shared connector), so unload must close it.
        mock_session.close.assert_called_once()


async def test_persist_failure_triggers_reauth(hass: HomeAssistant) -> None:
    """SAST-009: a failed config-entry update must start the reauth flow.

    When Auth0 has rotated the refresh token but our persist call to
    `async_update_entry` fails, in-memory state diverges from disk: the next HA
    restart loads a stale (revoked) token. Surface that immediately rather than
    letting the user discover it on next boot.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_ENTRY_DATA,
        unique_id="1234567890_9999999999",
    )
    entry.add_to_hass(hass)

    mock_session = MagicMock()
    mock_session.close = AsyncMock()

    with (
        patch(
            "custom_components.haggle.aiohttp.ClientSession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.haggle.agl.client.AglAuth.async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="access_token",
        ),
        patch(
            "custom_components.haggle.coordinator.HaggleCoordinator._async_setup",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.haggle.coordinator.HaggleCoordinator._async_update_data",
            new_callable=AsyncMock,
            return_value=_COORDINATOR_DATA,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        persist = entry.runtime_data.auth._persist

        with (
            patch.object(
                hass.config_entries,
                "async_update_entry",
                side_effect=RuntimeError("storage layer unavailable"),
            ),
            patch.object(entry, "async_start_reauth") as mock_start_reauth,
        ):
            await persist("v1.rotated_new_token")

        mock_start_reauth.assert_called_once_with(hass)


async def test_pin_mismatch_emits_persistent_notification(hass: HomeAssistant) -> None:
    """SPKI mismatch must surface via HA persistent notification, not block the request.

    Closes AP-1: a LAN-MITM serving a different cert post-install is observable
    through this notification path. Match path stays silent.
    """
    from custom_components.haggle.agl.pinning import AGL_AUTH_HOST_NAME
    from custom_components.haggle.const import (
        CONF_PINNED_SPKI_AUTH,
        CONF_PINNED_SPKI_BFF,
    )

    pinned_data = {
        **_ENTRY_DATA,
        CONF_PINNED_SPKI_AUTH: "a" * 64,
        CONF_PINNED_SPKI_BFF: "b" * 64,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=pinned_data,
        unique_id="1234567890_9999999999",
    )
    entry.add_to_hass(hass)

    mock_session = MagicMock()
    mock_session.close = AsyncMock()

    with (
        patch(
            "custom_components.haggle.aiohttp.ClientSession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.haggle.agl.client.AglAuth.async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="access_token",
        ),
        patch(
            "custom_components.haggle.coordinator.HaggleCoordinator._async_setup",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.haggle.coordinator.HaggleCoordinator._async_update_data",
            new_callable=AsyncMock,
            return_value=_COORDINATOR_DATA,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        pin_check = entry.runtime_data.connector.on_new_connection
        assert pin_check is not None

        # Match: no notification.
        with patch(
            "custom_components.haggle.persistent_notification.async_create"
        ) as mock_notify:
            pin_check(AGL_AUTH_HOST_NAME, "a" * 64)
        mock_notify.assert_not_called()

        # Mismatch: notification fires with deterministic notification_id.
        with patch(
            "custom_components.haggle.persistent_notification.async_create"
        ) as mock_notify:
            pin_check(AGL_AUTH_HOST_NAME, "f" * 64)
        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["notification_id"] == f"haggle_pin_mismatch_{AGL_AUTH_HOST_NAME}"
        assert AGL_AUTH_HOST_NAME in kwargs["message"]


async def test_pin_check_with_no_pin_stays_silent(hass: HomeAssistant) -> None:
    """Legacy entries (pre-TOFU, empty stored pins) must not warn on every poll."""
    from custom_components.haggle.agl.pinning import AGL_AUTH_HOST_NAME

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_ENTRY_DATA,  # no CONF_PINNED_SPKI_* keys
        unique_id="legacy",
    )
    entry.add_to_hass(hass)

    mock_session = MagicMock()
    mock_session.close = AsyncMock()

    with (
        patch(
            "custom_components.haggle.aiohttp.ClientSession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.haggle.agl.client.AglAuth.async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="access_token",
        ),
        patch(
            "custom_components.haggle.coordinator.HaggleCoordinator._async_setup",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.haggle.coordinator.HaggleCoordinator._async_update_data",
            new_callable=AsyncMock,
            return_value=_COORDINATOR_DATA,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        pin_check = entry.runtime_data.connector.on_new_connection
        assert pin_check is not None
        with patch(
            "custom_components.haggle.persistent_notification.async_create"
        ) as mock_notify:
            pin_check(AGL_AUTH_HOST_NAME, "anything")
        mock_notify.assert_not_called()


def test_device_info_does_not_claim_agl_authorship() -> None:
    """HA's Service-info card renders DeviceInfo as `model by manufacturer`.

    This is an unofficial third-party integration, so neither field may
    contain `AGL Australia` or claim AGL as the integration author. AGL
    is the upstream service the integration *talks to*, not the publisher.
    A future contributor restoring `manufacturer="AGL Australia"` or
    `model="AGL Energy API"` would silently re-introduce the v0.1.0 leak.
    """
    from custom_components.haggle.sensor import HaggleEnergySensor

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_ENTRY_DATA,
        unique_id="1234567890_9999999999",
        title="61 Sample Street SAMPLEVILLE QLD 4000",
    )
    coord = MagicMock()
    desc = MagicMock()
    desc.key = "latest_cumulative_kwh"
    sensor = HaggleEnergySensor(coord, entry, desc)
    info = sensor._attr_device_info
    assert info is not None
    manufacturer = info.get("manufacturer")
    model = info.get("model") or ""

    # Hard-line: we do not claim AGL identity.
    assert manufacturer != "AGL Australia"
    assert "AGL Australia" not in (manufacturer or "")
    assert model != "AGL Energy API"

    # Soft-line: the disclaimer must be visible somewhere in the device card.
    assert manufacturer == "Haggle"
    assert "unofficial" in model.lower()
