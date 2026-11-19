"""Tests for the diagnostics."""

from __future__ import annotations

import json

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.post_at.diagnostics import async_get_config_entry_diagnostics

from .conftest import ACCOUNT_ID, EMAIL


async def test_diagnostics_redacted(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Tokens and personal data are redacted, tracking states are kept."""
    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)
    dump = json.dumps(diagnostics)

    for secret in (
        "refresh-1",
        "signature",
        EMAIL,
        ACCOUNT_ID,
        "Headphones",
        "Shop GmbH",
        "Max Mustermann",
        "Hauptstraße",
        "Garage",
        "Wien",
        '"1001"',
    ):
        assert secret not in dump, secret

    assert diagnostics["entry"]["data"]["session"] == REDACTED
    assert diagnostics["entry"]["title"] == REDACTED
    assert diagnostics["last_update_success"] is True
    data = diagnostics["data"]
    assert data["shipments"]["total"] == 6
    shipment = data["shipments"]["shipments"][0]
    assert shipment["tracking_number"] == REDACTED
    assert shipment["sender"] == REDACTED
    assert shipment["events"][-1]["postal_code"] == REDACTED
    assert shipment["events"][-1]["state_key"] == "inDelivery"
    assert shipment["events"][-1]["description"] == "Event inDelivery"
    assert data["shipments"]["shipments"][3]["recipient"] == REDACTED
    assert data["shipments"]["shipments"][3]["notification"]["claim"] == "ABHOLBEREIT"
    assert [r["tracking_number"] for r in data["redirections"]] == [REDACTED] * 3
    assert data["branch_names"] == {"101": "Postfiliale Landstraße"}
    assert data["places"][0] == {"key": "Vor_Haustüre", "text": "Vor der Haustüre"}
