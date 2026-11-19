"""Tests for the sensors."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from homeassistant.components.sensor import ATTR_OPTIONS, SensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_FRIENDLY_NAME, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.post_at.const import CONF_KEEP_DELIVERED_DAYS, DOMAIN

from .conftest import ACCOUNT_ID, EMAIL, entity_id, shipment_data, shipment_list


def _shipment_entity_ids(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    prefix = f"{ACCOUNT_ID}_shipment_"
    return {
        entry.unique_id.removeprefix(prefix): entry.entity_id
        for entry in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if entry.unique_id.startswith(prefix)
    }


async def test_summary_sensors(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """The account sensors count incoming, outgoing and ready-for-pickup shipments."""
    incoming = hass.states.get(entity_id(hass, "incoming"))
    assert incoming.state == "3"
    assert incoming.attributes[ATTR_FRIENDLY_NAME] == f"{EMAIL} Incoming shipments"
    assert [s["tracking_number"] for s in incoming.attributes["shipments"]] == [
        "1001",
        "1002",
        "1004",
    ]
    first = incoming.attributes["shipments"][0]
    assert first["title"] == "Headphones"
    assert first["sender"] == "Shop GmbH"
    assert first["state"] == "in_delivery"
    assert first["state_text"] == "In Zustellung"
    assert first["estimated_delivery"] == "Mo, 23.11."
    assert first["last_update"]

    outgoing = hass.states.get(entity_id(hass, "outgoing"))
    assert outgoing.state == "1"
    assert [s["tracking_number"] for s in outgoing.attributes["shipments"]] == ["1003"]

    pickup = hass.states.get(entity_id(hass, "ready_for_pickup"))
    assert pickup.state == "1"
    assert "shipments" not in pickup.attributes

    device_id = er.async_get(hass).async_get(entity_id(hass, "incoming")).device_id
    device = dr.async_get(hass).async_get(device_id)
    assert device.identifiers == {(DOMAIN, ACCOUNT_ID)}
    assert device.name == EMAIL
    assert device.entry_type is dr.DeviceEntryType.SERVICE


async def test_shipment_sensors(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Every active or recently delivered shipment gets an enum sensor."""
    entity_ids = _shipment_entity_ids(hass, init_integration)
    # 1006 was delivered longer ago than keep_delivered_days.
    assert sorted(entity_ids) == ["1001", "1002", "1003", "1004", "1005"]

    state = hass.states.get(entity_ids["1001"])
    assert state.state == "in_delivery"
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.ENUM
    assert state.attributes[ATTR_OPTIONS] == [
        "aviso",
        "delivery_hand_over",
        "in_distribution",
        "in_delivery",
        "delivered",
        "unknown",
    ]
    assert state.attributes[ATTR_FRIENDLY_NAME] == f"{EMAIL} Headphones"
    assert state.attributes["tracking_number"] == "1001"
    assert state.attributes["sender"] == "Shop GmbH"
    assert state.attributes["title"] == "Headphones"
    assert state.attributes["direction"] == "incoming"
    assert state.attributes["state_key"] == "inDelivery"
    assert state.attributes["state_text"] == "In Zustellung"
    assert state.attributes["last_event_description"] == "Event inDelivery"
    assert state.attributes["last_event_timestamp"]
    assert state.attributes["estimated_delivery"] == "Mo, 23.11."
    assert state.attributes["delivery_type"] == "DELIVERY"
    assert state.attributes["weight"] == 1.5
    assert state.attributes["redirection"] == "place"

    # Named after the sender without a title, after the tracking number without either.
    assert hass.states.get(entity_ids["1002"]).attributes[ATTR_FRIENDLY_NAME] == (
        f"{EMAIL} Shop GmbH"
    )
    assert hass.states.get(entity_ids["1004"]).attributes[ATTR_FRIENDLY_NAME] == f"{EMAIL} 1004"
    assert hass.states.get(entity_ids["1002"]).attributes["redirection"] is None

    outgoing = hass.states.get(entity_ids["1003"])
    assert outgoing.state == "in_distribution"
    assert outgoing.attributes["direction"] == "outgoing"
    assert hass.states.get(entity_ids["1005"]).state == "delivered"


async def test_unknown_state_key(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Keys the integration does not know map to unknown; the raw key stays available."""
    mock_client.shipments.return_value = shipment_list(
        shipment_data("2001", "aviso", "customsClearance", title="Parcel"),
        shipment_data("2002", "deliveryHandOver", title="Letter"),
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity_ids = _shipment_entity_ids(hass, mock_config_entry)
    state = hass.states.get(entity_ids["2001"])
    assert state.state == STATE_UNKNOWN
    assert state.attributes["state_key"] == "customsClearance"
    assert hass.states.get(entity_ids["2002"]).state == "delivery_hand_over"


async def test_dynamic_shipment_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Sensors follow the shipment list: new ones are added, dropped ones removed."""
    registry = er.async_get(hass)
    before = _shipment_entity_ids(hass, init_integration)

    mock_client.shipments.return_value = shipment_list(
        shipment_data("1001", "aviso", "inDelivery", title="New headphones"),
        shipment_data("1002", "aviso", "delivered", age=timedelta(days=5)),
        shipment_data("1007", "aviso", title="Shoes"),
    )
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()

    after = _shipment_entity_ids(hass, init_integration)
    assert sorted(after) == ["1001", "1007"]
    assert after["1001"] == before["1001"]
    for number in ("1002", "1003", "1004", "1005"):
        assert registry.async_get(before[number]) is None
        assert hass.states.get(before[number]) is None
    assert hass.states.get(after["1007"]).state == "aviso"
    # Renaming a shipment renames its sensor.
    assert hass.states.get(after["1001"]).attributes[ATTR_FRIENDLY_NAME] == (
        f"{EMAIL} New headphones"
    )
    assert hass.states.get(entity_id(hass, "incoming")).state == "2"

    # A shipment that comes back gets its sensor again.
    mock_client.shipments.return_value = shipment_list(
        shipment_data("1001", "aviso", "inDelivery", title="New headphones"),
        shipment_data("1003", "inDistribution", title="Gift", incoming=False),
    )
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert sorted(_shipment_entity_ids(hass, init_integration)) == ["1001", "1003"]
    assert hass.states.get(_shipment_entity_ids(hass, init_integration)["1003"]).state == (
        "in_distribution"
    )


async def test_keep_delivered_days_option(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """keep_delivered_days controls how long delivered shipments keep a sensor."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_KEEP_DELIVERED_DAYS: 0}
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert sorted(_shipment_entity_ids(hass, mock_config_entry)) == ["1001", "1002", "1003", "1004"]


async def test_stale_registry_entries_removed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Sensors of shipments gone while Home Assistant was stopped are removed at setup."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{ACCOUNT_ID}_shipment_0999",
        config_entry=mock_config_entry,
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(stale.entity_id) is None
    assert entity_id(hass, "incoming") is not None
