"""Shared test fixtures.

`pytest-homeassistant-custom-component` provides the `hass` fixture and
the harness; we just enable custom-integration loading.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Enable loading of custom_components/ in every test."""
    return
