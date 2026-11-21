"""Tests for the parcel photo."""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import MagicMock

import respx

from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from .conftest import entity_id, shipment_data, shipment_list

PHOTO = "https://secure.post.at/storage/FMS|0001|aaaa-bbbb|PLC"
PHOTO_ESCAPED = "https://secure.post.at/storage/FMS%7C0001%7Caaaa-bbbb%7CPLC"
NEW_PHOTO = "https://secure.post.at/storage/FMS|0002|cccc-dddd|PLC"


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock, *shipments: dict
) -> None:
    client.shipments.return_value = shipment_list(*shipments)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@respx.mock
async def test_photo(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
) -> None:
    """Shipments with a photo get an image that serves it, with the URL escaped."""
    route = respx.get(PHOTO_ESCAPED).respond(
        content=b"jpeg", headers={"content-type": "image/jpeg"}
    )
    await _setup(
        hass,
        mock_config_entry,
        mock_client,
        shipment_data("4001", "inDistribution", bild=PHOTO),
        shipment_data("4002", "aviso"),
    )

    assert entity_id(hass, "photo_4002", "image") is None
    photo = entity_id(hass, "photo_4001", "image")
    state = hass.states.get(photo)
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Shop GmbH Photo"

    client = await hass_client()
    response = await client.get(f"/api/image_proxy/{photo}")
    assert response.status == HTTPStatus.OK
    assert await response.read() == b"jpeg"
    assert response.content_type == "image/jpeg"
    # The storage server resets the connection on an unescaped "|".
    assert b"%7C" in route.calls.last.request.url.raw_path


@respx.mock
async def test_photo_follows_shipment(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
) -> None:
    """A new photo replaces the cached one; a shipment without one loses the image."""
    respx.get(PHOTO_ESCAPED).respond(content=b"old", headers={"content-type": "image/jpeg"})
    respx.get(NEW_PHOTO.replace("|", "%7C")).respond(
        content=b"new", headers={"content-type": "image/jpeg"}
    )
    await _setup(
        hass, mock_config_entry, mock_client, shipment_data("4001", "inDistribution", bild=PHOTO)
    )
    photo = entity_id(hass, "photo_4001", "image")
    client = await hass_client()
    assert await (await client.get(f"/api/image_proxy/{photo}")).read() == b"old"
    first_update = hass.states.get(photo).state

    # An unchanged URL keeps the photo's time; a new one replaces the photo.
    coordinator = mock_config_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(photo).state == first_update
    mock_client.shipments.return_value = shipment_list(
        shipment_data("4001", "inDelivery", bild=NEW_PHOTO)
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(photo).state != first_update
    assert await (await client.get(f"/api/image_proxy/{photo}")).read() == b"new"

    mock_client.shipments.return_value = shipment_list(shipment_data("4001", "inDelivery"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert entity_id(hass, "photo_4001", "image") is None
    assert hass.states.get(photo) is None
