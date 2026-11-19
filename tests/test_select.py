"""Tests for the redirection selects."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

from post_at import GraphQLError
import pytest

from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache,
)

from .conftest import (
    default_shipments,
    entity_id,
    make_redirections,
    shipment_data,
    shipment_list,
)

PLACES = ["front_door", "apartment_door", "mailbox", "garage"]


def _delivery(hass: HomeAssistant, number: str = "1001") -> str:
    return entity_id(hass, f"delivery_option_{number}", SELECT_DOMAIN)


def _pickup(hass: HomeAssistant, number: str = "1001") -> str:
    return entity_id(hass, f"pickup_location_{number}", SELECT_DOMAIN)


def _redirections(mock_client: MagicMock, **kwargs: Any) -> None:
    """Return these redirection options for every shipment."""
    mock_client.possible_redirections.side_effect = lambda number: make_redirections(
        number, **kwargs
    )


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def _select(hass: HomeAssistant, entity: str, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity, ATTR_OPTION: option},
        blocking=True,
    )
    # Run the refresh requested after the redirect, also when it was debounced.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()


async def test_selects_only_for_incoming_undelivered(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Only incoming parcels not delivered yet can be redirected."""
    for number in ("1001", "1002", "1004"):
        assert _delivery(hass, number) is not None
        assert _pickup(hass, number) is not None
    for number in ("1003", "1005", "1006"):
        assert _delivery(hass, number) is None
        assert _pickup(hass, number) is None


async def test_delivery_option_standard(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Standard delivery with the drop-off places on offer."""
    state = hass.states.get(_delivery(hass))
    assert state.state == "standard"
    assert state.attributes[ATTR_OPTIONS] == ["standard", *PLACES]
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Headphones Delivery option"
    assert state.attributes["redirect_possible"] is True
    assert state.attributes["unavailability_reasons"] == {
        "NEIGHBOR": ["SIGNATURE"],
        "PLACE": [],
        "POST_OFFICE": [],
        "PARCEL_LOCKER": [],
        "DEFAULT": [],
    }


async def test_delivery_option_place_and_reset(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Choosing a place redirects there; standard resets the place redirection."""
    entity = _delivery(hass)
    mock_client.shipments.reset_mock()
    _redirections(mock_client, current="PLACE")
    await _select(hass, entity, "front_door")
    mock_client.redirect_to_place.assert_called_once_with("1001", "Vor_Haustüre")
    mock_client.shipments.assert_called_once()
    # The API does not say which place is set; the select remembers its choice.
    assert hass.states.get(entity).state == "front_door"

    await _select(hass, entity, "front_door")
    mock_client.redirect_to_place.assert_called_once()

    _redirections(mock_client)
    await _select(hass, entity, "standard")
    mock_client.reset_redirection.assert_called_once_with("1001", "place")
    assert hass.states.get(entity).state == "standard"

    # Already standard: nothing to reset.
    await _select(hass, entity, "standard")
    mock_client.reset_redirection.assert_called_once()

    # A place set elsewhere (app, action) is shown as an unknown drop-off place.
    _redirections(mock_client, current="PLACE")
    await _refresh(hass, init_integration)
    state = hass.states.get(entity)
    assert state.state == "drop_off_place"
    assert state.attributes[ATTR_OPTIONS] == ["standard", *PLACES, "drop_off_place"]


@pytest.mark.parametrize(
    ("current", "option", "kind"),
    [
        ("NEIGHBOR", "neighbor", "neighbor"),
        ("POST_OFFICE", "post_office", "post_office"),
        ("PARCEL_LOCKER", "parcel_locker", "parcel_locker"),
        ("PLACE", "drop_off_place", "place"),
    ],
)
async def test_delivery_option_read_only(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    current: str,
    option: str,
    kind: str,
) -> None:
    """Redirections set elsewhere are shown, cannot be chosen, but can be reset."""
    _redirections(mock_client, current=current, place="DISABLED")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity = _delivery(hass)
    state = hass.states.get(entity)
    assert state.state == option
    assert state.attributes[ATTR_OPTIONS] == ["standard", option]

    with pytest.raises(ServiceValidationError) as err:
        await _select(hass, entity, option)
    assert err.value.translation_key == "option_not_selectable"
    assert err.value.translation_placeholders == {"option": option}

    await _select(hass, entity, "standard")
    mock_client.reset_redirection.assert_called_once_with("1001", kind)
    mock_client.redirect_to_place.assert_not_called()


async def test_delivery_option_place_disabled(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Without the place redirection only standard delivery is offered."""
    _redirections(mock_client, place="DISABLED", post_office="DISABLED", parcel_locker="DISABLED")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(_delivery(hass))
    assert state.state == "standard"
    assert state.attributes[ATTR_OPTIONS] == ["standard"]
    assert state.attributes["redirect_possible"] is False
    assert state.attributes["unavailability_reasons"]["PLACE"] == ["SIGNATURE"]
    with pytest.raises(ServiceValidationError):
        await _select(hass, _delivery(hass), "front_door")
    mock_client.redirect_to_place.assert_not_called()


async def test_delivery_option_restored(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The place chosen before a restart is restored while a place is set."""
    mock_restore_cache(hass, [State("select.headphones_delivery_option", "garage")])
    _redirections(mock_client, current="PLACE")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _delivery(hass) == "select.headphones_delivery_option"
    assert hass.states.get(_delivery(hass)).state == "garage"


async def test_redirection_options_failure_tolerated(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A failing shipment makes its selects unavailable, not the whole update."""

    def possible_redirections(number: str) -> Any:
        if number == "1002":
            raise GraphQLError([{"message": "not available"}], None)
        return make_redirections(number)

    mock_client.possible_redirections.side_effect = possible_redirections
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.runtime_data.last_update_success
    assert hass.states.get(_delivery(hass, "1002")).state == STATE_UNAVAILABLE
    assert hass.states.get(_pickup(hass, "1002")).state == STATE_UNAVAILABLE
    assert hass.states.get(_delivery(hass, "1001")).state == "standard"
    assert hass.states.get(entity_id(hass, "incoming")).state == "3"


async def test_select_library_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Library errors are raised as translated errors."""
    mock_client.redirect_to_place.side_effect = GraphQLError([{"message": "no"}], None)
    with pytest.raises(HomeAssistantError) as err:
        await _select(hass, _delivery(hass), "mailbox")
    assert err.value.translation_key == "api_error"
    assert hass.states.get(_delivery(hass)).state == "standard"


async def test_pickup_location(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Branches are offered by name, the nearest one when the API lists none."""
    entity = _pickup(hass)
    state = hass.states.get(entity)
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_OPTIONS] == [
        "Post office: Postfiliale Landstraße",
        "Nearest parcel locker",
    ]
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Headphones Pickup location"

    await _select(hass, entity, "Post office: Postfiliale Landstraße")
    mock_client.redirect_to_post_office.assert_called_once_with("1001", "101")

    await _select(hass, entity, "Nearest parcel locker")
    mock_client.redirect_to_parcel_locker.assert_called_once_with("1001", None)


async def test_pickup_location_current(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The branch the parcel is redirected to is the current option."""
    data = default_shipments().model_dump(by_alias=True, mode="json")
    data["sendungen"][0]["packageRedirections"] = [
        {"postoffice": {"selectedBranch": "102", "isSelected": True}}
    ]
    mock_client.shipments.return_value = type(default_shipments()).model_validate(data)
    _redirections(
        mock_client,
        current="POST_OFFICE",
        parcel_locker="DISABLED",
        post_office_branches=["101"],
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity = _pickup(hass)
    state = hass.states.get(entity)
    assert state.state == "Post office: 102"
    assert state.attributes[ATTR_OPTIONS] == [
        "Post office: Postfiliale Landstraße",
        "Post office: 102",
    ]
    assert hass.states.get(_delivery(hass)).state == "post_office"

    await _select(hass, entity, "Post office: 102")
    mock_client.redirect_to_post_office.assert_not_called()


async def test_pickup_location_unavailable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Without post office and parcel locker redirections the select is unavailable."""
    _redirections(mock_client, post_office="DISABLED", parcel_locker="DISABLED")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(_pickup(hass)).state == STATE_UNAVAILABLE
    assert hass.states.get(_delivery(hass)).state == "standard"


async def test_pickup_location_german(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Branch options follow the language of Home Assistant."""
    hass.config.language = "de"
    _redirections(mock_client, parcel_locker_branches=["L1"], post_office="DISABLED")
    mock_client.branches.return_value = []
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(_pickup(hass)).attributes[ATTR_OPTIONS] == ["Abholstation: L1"]

    _redirections(mock_client, post_office="ENABLED", parcel_locker="DISABLED")
    await _refresh(hass, mock_config_entry)
    assert hass.states.get(_pickup(hass)).attributes[ATTR_OPTIONS] == ["Nächste Postfiliale"]


async def test_selects_removed_when_delivered(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Delivered parcels keep their status sensor but lose their selects."""
    delivery, pickup = _delivery(hass), _pickup(hass)
    mock_client.shipments.return_value = shipment_list(
        shipment_data("1001", "inDelivery", "delivered", title="Headphones"),
        shipment_data("1002", "aviso"),
    )
    await _refresh(hass, init_integration)
    assert hass.states.get(delivery) is None
    assert hass.states.get(pickup) is None
    assert _delivery(hass) is None
    assert hass.states.get(entity_id(hass, "shipment_1001")).state == "delivered"
    assert _delivery(hass, "1002") is not None
