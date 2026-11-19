"""Tests for the actions."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
from post_at import ApiError, AuthenticationError, GraphQLError
from post_at.models import Place, Shipment, ShipmentRedirections
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.post_at.const import CONF_SESSION, DOMAIN

from .conftest import load_fixture, make_session, shipment_data

NUMBER = "1001"


async def _call(
    hass: HomeAssistant, service: str, data: dict[str, Any], *, response: bool = False
) -> Any:
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=response
    )


@pytest.mark.parametrize(
    ("data", "method", "args"),
    [
        (
            {"target": "place", "place": "Vor_Haustüre"},
            "redirect_to_place",
            (NUMBER, "Vor_Haustüre", None),
        ),
        (
            {"target": "place", "place": "sonstige", "description": "Behind the gate"},
            "redirect_to_place",
            (NUMBER, "sonstige", "Behind the gate"),
        ),
        (
            {
                "target": "neighbor",
                "first_name": "Maria",
                "last_name": "Muster",
                "street": "Musterstraße",
                "house_number": "12",
                "door": "4",
            },
            "redirect_to_neighbor",
            (NUMBER, "Maria", "Muster", "Musterstraße", "12", "4"),
        ),
        (
            {"target": "neighbor", "first_name": "Maria", "last_name": "Muster", "street": "Weg"},
            "redirect_to_neighbor",
            (NUMBER, "Maria", "Muster", "Weg", None, None),
        ),
        ({"target": "post_office"}, "redirect_to_post_office", (NUMBER, None)),
        (
            {"target": "post_office", "branch_key": "1030"},
            "redirect_to_post_office",
            (NUMBER, "1030"),
        ),
        ({"target": "parcel_locker"}, "redirect_to_parcel_locker", (NUMBER, None)),
        (
            {"target": "parcel_locker", "branch_key": "L-1"},
            "redirect_to_parcel_locker",
            (NUMBER, "L-1"),
        ),
    ],
)
async def test_redirect_shipment(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    data: dict[str, Any],
    method: str,
    args: tuple[Any, ...],
) -> None:
    """Each target calls its library method and refreshes the shipments."""
    mock_client.shipments.reset_mock()
    await _call(hass, "redirect_shipment", {"tracking_number": NUMBER, **data})
    await hass.async_block_till_done()
    getattr(mock_client, method).assert_called_once_with(*args)
    mock_client.shipments.assert_called_once()


@pytest.mark.parametrize(
    ("data", "key", "fields"),
    [
        ({"target": "place"}, "missing_fields", "place"),
        ({"target": "place", "place": ""}, "missing_fields", "place"),
        ({"target": "neighbor", "first_name": "Maria"}, "missing_fields", "last_name, street"),
        (
            {"target": "place", "place": "In_Garage", "branch_key": "1030"},
            "unexpected_fields",
            "branch_key",
        ),
        (
            {"target": "post_office", "first_name": "Maria", "description": "x"},
            "unexpected_fields",
            "description, first_name",
        ),
    ],
)
async def test_redirect_shipment_validation(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    data: dict[str, Any],
    key: str,
    fields: str,
) -> None:
    """Fields are validated per target before anything is sent."""
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "redirect_shipment", {"tracking_number": NUMBER, **data})
    assert err.value.translation_key == key
    assert err.value.translation_placeholders == {"target": data["target"], "fields": fields}
    for method in (
        "redirect_to_place",
        "redirect_to_neighbor",
        "redirect_to_post_office",
        "redirect_to_parcel_locker",
    ):
        getattr(mock_client, method).assert_not_called()


async def test_redirect_shipment_invalid_target(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Unknown targets are rejected by the schema."""
    with pytest.raises(Exception, match="target"):
        await _call(hass, "redirect_shipment", {"tracking_number": NUMBER, "target": "moon"})


@pytest.mark.parametrize("target", ["place", "neighbor", "post_office", "parcel_locker"])
async def test_reset_redirection(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock, target: str
) -> None:
    """Resetting a redirection passes the target as kind."""
    await _call(hass, "reset_redirection", {"tracking_number": NUMBER, "target": target})
    mock_client.reset_redirection.assert_called_once_with(NUMBER, target)


@pytest.mark.parametrize(
    ("service", "data", "method", "args", "kwargs"),
    [
        ("add_shipment", {}, "add_shipment", (NUMBER,), {"outbound": False}),
        ("add_shipment", {"outbound": True}, "add_shipment", (NUMBER,), {"outbound": True}),
        ("remove_shipment", {}, "remove_shipment", (NUMBER,), {"outbound": False}),
        (
            "remove_shipment",
            {"outbound": True},
            "remove_shipment",
            (NUMBER,),
            {"outbound": True},
        ),
        ("rename_shipment", {"title": "Shoes"}, "rename_shipment", (NUMBER, "Shoes"), {}),
    ],
)
async def test_shipment_actions(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    service: str,
    data: dict[str, Any],
    method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    """Add, remove and rename call the library and refresh."""
    mock_client.shipments.reset_mock()
    await _call(hass, service, {"tracking_number": NUMBER, **data})
    await hass.async_block_till_done()
    getattr(mock_client, method).assert_called_once_with(*args, **kwargs)
    mock_client.shipments.assert_called_once()


async def test_get_redirection_options(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The options and drop-off places are returned with plain values."""
    mock_client.possible_redirections.return_value = ShipmentRedirections.model_validate(
        load_fixture("possible_redirections.json")
    )
    mock_client.places.return_value = [
        Place(key="Vor_Haustüre", text="Vor der Haustüre"),
        Place(key="sonstige", text="Sonstiger Ort"),
    ]
    response = await _call(
        hass, "get_redirection_options", {"tracking_number": NUMBER}, response=True
    )
    mock_client.possible_redirections.assert_called_once_with(NUMBER)
    assert response["options"][1] == {
        "type": "PLACE",
        "state": "DISABLED",
        "current": True,
        "unavailability_reasons": ["DROP_OFF"],
    }
    assert [option["type"] for option in response["options"]] == [
        "NEIGHBOR",
        "PLACE",
        "POST_OFFICE",
        "PARCEL_LOCKER",
        "DEFAULT",
    ]
    assert response["options"][4]["state"] == "ENABLED"
    assert response["places"] == [
        {"key": "Vor_Haustüre", "text": "Vor der Haustüre"},
        {"key": "sonstige", "text": "Sonstiger Ort"},
    ]
    assert all(type(option["type"]) is str for option in response["options"])


async def test_get_redirection_options_not_found(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Unknown shipments raise a validation error."""
    mock_client.possible_redirections.return_value = None
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "get_redirection_options", {"tracking_number": "0"}, response=True)
    assert err.value.translation_key == "shipment_not_found"


async def test_track_shipment(
    hass: HomeAssistant, mock_client_class: MagicMock, mock_client: MagicMock
) -> None:
    """Public tracking needs no account and returns snake_case fields."""
    assert await async_setup_component(hass, DOMAIN, {})
    mock_client.track.return_value = Shipment.model_validate(
        shipment_data("1001", "inDelivery", "delivered", weight=0.1)
    )
    response = await _call(hass, "track_shipment", {"tracking_number": NUMBER}, response=True)
    mock_client.track.assert_called_once_with(NUMBER)
    mock_client_class.assert_called_once_with()
    mock_client.close.assert_not_called()
    mock_client.__exit__.assert_called_once()
    assert response["tracking_number"] == NUMBER
    assert response["latest_event"]["description"] == "Event delivered"
    assert response["latest_event"]["state_key"] == "delivered"
    assert response["events"][0]["state_key"] == "inDelivery"
    assert response["weight"] == 0.1
    assert "activeIdentityCode" not in response

    mock_client.track.return_value = None
    assert await _call(hass, "track_shipment", {"tracking_number": "0"}, response=True) == {}


async def test_track_shipment_error(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """Tracking errors are raised as Home Assistant errors."""
    assert await async_setup_component(hass, DOMAIN, {})
    mock_client.track.side_effect = httpx.ConnectError("unreachable")
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "track_shipment", {"tracking_number": NUMBER}, response=True)
    assert err.value.translation_key == "communication_error"


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (AuthenticationError("invalid_grant"), "auth_failed"),
        (GraphQLError([{"message": "not redirectable"}], None), "api_error"),
        (ApiError(500, "boom", "op"), "api_error"),
        (httpx.ReadTimeout("timeout"), "communication_error"),
    ],
)
async def test_library_errors(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    error: Exception,
    key: str,
) -> None:
    """Library errors become translated Home Assistant errors."""
    mock_client.redirect_to_post_office.side_effect = error
    with pytest.raises(HomeAssistantError) as err:
        await _call(
            hass, "redirect_shipment", {"tracking_number": NUMBER, "target": "post_office"}
        )
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == key


async def test_entry_selection(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """config_entry_id is needed with several accounts and must name a loaded one."""
    second = MockConfigEntry(
        domain=DOMAIN,
        title="other@example.com",
        unique_id="sso-other",
        data={CONF_SESSION: make_session("other").to_dict()},
    )
    second.add_to_hass(hass)
    await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "rename_shipment", {"tracking_number": NUMBER, "title": "x"})
    assert err.value.translation_key == "multiple_entries"

    await _call(
        hass,
        "rename_shipment",
        {"config_entry_id": second.entry_id, "tracking_number": NUMBER, "title": "x"},
    )
    mock_client.rename_shipment.assert_called_once_with(NUMBER, "x")

    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "rename_shipment",
            {"config_entry_id": "missing", "tracking_number": NUMBER, "title": "x"},
        )
    assert err.value.translation_key == "entry_not_found"

    await hass.config_entries.async_unload(second.entry_id)
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "rename_shipment",
            {"config_entry_id": second.entry_id, "tracking_number": NUMBER, "title": "x"},
        )
    assert err.value.translation_key == "entry_not_loaded"

    await hass.config_entries.async_unload(init_integration.entry_id)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "rename_shipment", {"tracking_number": NUMBER, "title": "x"})
    assert err.value.translation_key == "no_loaded_entries"
