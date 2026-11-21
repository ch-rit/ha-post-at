"""Tests for the parcel devices and the redirection data of the coordinator."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import httpx
from post_at import ApiError

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.post_at.const import DOMAIN

from .conftest import (
    ACCOUNT_ID,
    entity_id,
    make_branch,
    make_redirections,
    shipment_data,
    shipment_list,
)


def _parcel_device(
    hass: HomeAssistant, entry: MockConfigEntry, number: str
) -> dr.DeviceEntry | None:
    identifier = (DOMAIN, f"{entry.entry_id}_{number}")
    return next(
        (
            device
            for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
            if identifier in device.identifiers
        ),
        None,
    )


async def test_parcel_devices(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Every visible shipment has a device below the account device."""
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    account = entities.async_get(entity_id(hass, "incoming")).device_id

    device = _parcel_device(hass, init_integration, "1001")
    assert device.name == "Headphones"
    assert device.model == "Paket"
    assert device.manufacturer == "Österreichische Post AG"
    assert device.entry_type is dr.DeviceEntryType.SERVICE
    assert device.via_device_id == account
    assert {e.unique_id for e in er.async_entries_for_device(entities, device.id)} == {
        f"{ACCOUNT_ID}_shipment_1001",
        f"{ACCOUNT_ID}_location_1001",
        f"{ACCOUNT_ID}_delivery_option_1001",
        f"{ACCOUNT_ID}_pickup_location_1001",
    }

    assert _parcel_device(hass, init_integration, "1002").name == "Shop GmbH"
    assert _parcel_device(hass, init_integration, "1004").name == "1004"
    # Outgoing and recently delivered parcels only have their sensors.
    for number in ("1003", "1005"):
        device = _parcel_device(hass, init_integration, number)
        assert {e.unique_id for e in er.async_entries_for_device(entities, device.id)} == {
            f"{ACCOUNT_ID}_shipment_{number}",
            f"{ACCOUNT_ID}_location_{number}",
        }
    assert _parcel_device(hass, init_integration, "1006") is None
    assert devices.async_get(account).name == "user@example.com"


async def test_parcel_devices_follow_shipments(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Devices are renamed with their shipment and removed when it drops out."""
    entities = er.async_get(hass)
    removed = _parcel_device(hass, init_integration, "1002")
    mock_client.shipments.return_value = shipment_list(
        shipment_data("1001", "aviso", title="Renamed"),
        shipment_data("1007", "aviso", title="Shoes"),
    )
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert _parcel_device(hass, init_integration, "1001").name == "Renamed"
    assert _parcel_device(hass, init_integration, "1007").name == "Shoes"
    for number in ("1002", "1003", "1004", "1005"):
        assert _parcel_device(hass, init_integration, number) is None
    assert dr.async_get(hass).async_get(removed.id) is None
    assert not er.async_entries_for_device(entities, removed.id)
    assert entity_id(hass, "shipment_1002") is None


async def test_redirections_fetched_for_redirectable_only(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Redirection options are fetched for incoming, undelivered shipments only."""
    assert sorted(c.args[0] for c in mock_client.possible_redirections.call_args_list) == [
        "1001",
        "1002",
        "1004",
    ]
    data = init_integration.runtime_data.data
    assert sorted(data.redirections) == ["1001", "1002", "1004"]
    assert data.places[0].key == "Vor_Haustüre"


async def test_places_and_branch_names_cached(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Places are fetched once and branch names only for keys not seen before."""
    coordinator = init_integration.runtime_data
    mock_client.branches.assert_called_once_with(branch_keys=["101"])

    mock_client.possible_redirections.side_effect = lambda number: make_redirections(
        number, post_office_branches=["101", "102"], parcel_locker_branches=["L1"]
    )
    mock_client.branches.return_value = [
        make_branch("102", "Postfiliale Erdberg"),
        make_branch("L1", "Abholstation Rochusmarkt"),
    ]
    await coordinator.async_refresh()
    mock_client.branches.assert_called_with(branch_keys=["102", "L1"])
    assert coordinator.data.branch_names == {
        "101": "Postfiliale Landstraße",
        "102": "Postfiliale Erdberg",
        "L1": "Abholstation Rochusmarkt",
    }

    await coordinator.async_refresh()
    assert mock_client.branches.call_count == 2
    mock_client.places.assert_called_once_with()


async def test_places_retried_after_failure(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A failing places or branches call does not fail the update."""
    mock_client.places.side_effect = ApiError(500, "error", None)
    mock_client.branches.side_effect = ApiError(500, "error", None)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data
    assert coordinator.last_update_success
    assert coordinator.data.places == []
    assert coordinator.data.branch_names == {}

    mock_client.places.side_effect = None
    mock_client.branches.side_effect = None
    await coordinator.async_refresh()
    assert coordinator.data.places[0].key == "Vor_Haustüre"
    assert coordinator.data.branch_names == {"101": "Postfiliale Landstraße"}


async def test_transport_error_fails_update(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Connection errors while fetching redirection options fail the update."""
    coordinator = init_integration.runtime_data
    mock_client.possible_redirections.side_effect = httpx.ReadTimeout("timeout")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success


async def test_delivered_parcel_kept_for_keep_days(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A parcel delivered longer ago than keep_delivered_days loses its device."""
    mock_client.shipments.return_value = shipment_list(
        shipment_data("1001", "delivered", title="Headphones", age=timedelta(days=4)),
    )
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _parcel_device(hass, init_integration, "1001") is None
