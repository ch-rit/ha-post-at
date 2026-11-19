"""Actions of the Österreichische Post integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

import httpx
from post_at import PostAtClient, PostAtError
from post_at.models import Shipment
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    TARGET_NEIGHBOR,
    TARGET_PARCEL_LOCKER,
    TARGET_PLACE,
    TARGET_POST_OFFICE,
    TARGETS,
)
from .coordinator import PostAtConfigEntry, PostAtCoordinator, library_error

SERVICE_REDIRECT_SHIPMENT: Final = "redirect_shipment"
SERVICE_RESET_REDIRECTION: Final = "reset_redirection"
SERVICE_GET_REDIRECTION_OPTIONS: Final = "get_redirection_options"
SERVICE_ADD_SHIPMENT: Final = "add_shipment"
SERVICE_REMOVE_SHIPMENT: Final = "remove_shipment"
SERVICE_RENAME_SHIPMENT: Final = "rename_shipment"
SERVICE_TRACK_SHIPMENT: Final = "track_shipment"

ATTR_TRACKING_NUMBER: Final = "tracking_number"
ATTR_TARGET: Final = "target"
ATTR_PLACE: Final = "place"
ATTR_DESCRIPTION: Final = "description"
ATTR_FIRST_NAME: Final = "first_name"
ATTR_LAST_NAME: Final = "last_name"
ATTR_STREET: Final = "street"
ATTR_HOUSE_NUMBER: Final = "house_number"
ATTR_DOOR: Final = "door"
ATTR_BRANCH_KEY: Final = "branch_key"
ATTR_OUTBOUND: Final = "outbound"
ATTR_TITLE: Final = "title"

# Per redirection target: (required fields, optional fields).
TARGET_FIELDS: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    TARGET_PLACE: ((ATTR_PLACE,), (ATTR_DESCRIPTION,)),
    TARGET_NEIGHBOR: (
        (ATTR_FIRST_NAME, ATTR_LAST_NAME, ATTR_STREET),
        (ATTR_HOUSE_NUMBER, ATTR_DOOR),
    ),
    TARGET_POST_OFFICE: ((), (ATTR_BRANCH_KEY,)),
    TARGET_PARCEL_LOCKER: ((), (ATTR_BRANCH_KEY,)),
}
REDIRECTION_FIELDS: Final = (
    ATTR_PLACE,
    ATTR_DESCRIPTION,
    ATTR_FIRST_NAME,
    ATTR_LAST_NAME,
    ATTR_STREET,
    ATTR_HOUSE_NUMBER,
    ATTR_DOOR,
    ATTR_BRANCH_KEY,
)

SHIPMENT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_TRACKING_NUMBER): cv.string,
    }
)
REDIRECT_SHIPMENT_SCHEMA = SHIPMENT_SCHEMA.extend(
    {
        vol.Required(ATTR_TARGET): vol.In(TARGETS),
        **{vol.Optional(field): cv.string for field in REDIRECTION_FIELDS},
    }
)
RESET_REDIRECTION_SCHEMA = SHIPMENT_SCHEMA.extend(
    {vol.Required(ATTR_TARGET): vol.In(TARGETS)}
)
ADD_REMOVE_SHIPMENT_SCHEMA = SHIPMENT_SCHEMA.extend(
    {vol.Optional(ATTR_OUTBOUND, default=False): cv.boolean}
)
RENAME_SHIPMENT_SCHEMA = SHIPMENT_SCHEMA.extend({vol.Required(ATTR_TITLE): cv.string})
TRACK_SHIPMENT_SCHEMA = vol.Schema({vol.Required(ATTR_TRACKING_NUMBER): cv.string})


@callback
def _async_get_coordinator(call: ServiceCall) -> PostAtCoordinator:
    """Return the coordinator of the selected, or the only loaded, account."""
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entry: PostAtConfigEntry | None = call.hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_found",
                translation_placeholders={"entry_id": entry_id},
            )
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
                translation_placeholders={"title": entry.title},
            )
        return entry.runtime_data
    entries: list[PostAtConfigEntry] = call.hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_loaded_entries"
        )
    if len(entries) > 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="multiple_entries"
        )
    return entries[0].runtime_data


async def _async_mutate(call: ServiceCall, func: Callable[[PostAtClient], Any]) -> None:
    """Change a shipment, then refresh the shipment list."""
    coordinator = _async_get_coordinator(call)
    await coordinator.async_run(func)
    await coordinator.async_request_refresh()


async def _async_redirect_shipment(call: ServiceCall) -> None:
    data = call.data
    number: str = data[ATTR_TRACKING_NUMBER]
    target: str = data[ATTR_TARGET]
    required, optional = TARGET_FIELDS[target]
    if missing := [field for field in required if not data.get(field)]:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="missing_fields",
            translation_placeholders={"target": target, "fields": ", ".join(missing)},
        )
    if unexpected := [
        field
        for field in REDIRECTION_FIELDS
        if data.get(field) and field not in required and field not in optional
    ]:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unexpected_fields",
            translation_placeholders={"target": target, "fields": ", ".join(unexpected)},
        )

    def redirect(client: PostAtClient) -> Any:
        if target == TARGET_PLACE:
            return client.redirect_to_place(
                number, data[ATTR_PLACE], data.get(ATTR_DESCRIPTION) or None
            )
        if target == TARGET_NEIGHBOR:
            return client.redirect_to_neighbor(
                number,
                data[ATTR_FIRST_NAME],
                data[ATTR_LAST_NAME],
                data[ATTR_STREET],
                data.get(ATTR_HOUSE_NUMBER) or None,
                data.get(ATTR_DOOR) or None,
            )
        if target == TARGET_POST_OFFICE:
            return client.redirect_to_post_office(number, data.get(ATTR_BRANCH_KEY) or None)
        return client.redirect_to_parcel_locker(number, data.get(ATTR_BRANCH_KEY) or None)

    await _async_mutate(call, redirect)


async def _async_reset_redirection(call: ServiceCall) -> None:
    number: str = call.data[ATTR_TRACKING_NUMBER]
    target: str = call.data[ATTR_TARGET]
    await _async_mutate(call, lambda client: client.reset_redirection(number, target))


async def _async_add_shipment(call: ServiceCall) -> None:
    number: str = call.data[ATTR_TRACKING_NUMBER]
    outbound: bool = call.data[ATTR_OUTBOUND]
    await _async_mutate(call, lambda client: client.add_shipment(number, outbound=outbound))


async def _async_remove_shipment(call: ServiceCall) -> None:
    number: str = call.data[ATTR_TRACKING_NUMBER]
    outbound: bool = call.data[ATTR_OUTBOUND]
    await _async_mutate(
        call, lambda client: client.remove_shipment(number, outbound=outbound)
    )


async def _async_rename_shipment(call: ServiceCall) -> None:
    number: str = call.data[ATTR_TRACKING_NUMBER]
    title: str = call.data[ATTR_TITLE]
    await _async_mutate(call, lambda client: client.rename_shipment(number, title))


async def _async_get_redirection_options(call: ServiceCall) -> ServiceResponse:
    number: str = call.data[ATTR_TRACKING_NUMBER]
    coordinator = _async_get_coordinator(call)
    cached_places = coordinator.data.places
    redirections, places = await coordinator.async_run(
        lambda client: (
            client.possible_redirections(number),
            cached_places or client.places(),
        )
    )
    if redirections is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="shipment_not_found",
            translation_placeholders={"tracking_number": number},
        )
    return {
        "options": [
            {
                "type": str(option.type),
                "state": str(option.state),
                "current": option.current,
                "unavailability_reasons": list(option.unavailability_reasons),
            }
            for option in redirections.options
        ],
        "places": [{"key": place.key, "text": place.text} for place in places],
    }


def _track(tracking_number: str) -> Shipment | None:
    with PostAtClient() as client:
        return client.track(tracking_number)


async def _async_track_shipment(call: ServiceCall) -> ServiceResponse:
    try:
        shipment = await call.hass.async_add_executor_job(
            _track, call.data[ATTR_TRACKING_NUMBER]
        )
    except (PostAtError, httpx.HTTPError) as err:
        raise library_error(err) from err
    if shipment is None:
        return {}
    result = shipment.model_dump(mode="json", by_alias=False)
    result["latest_event"] = result["events"][-1] if result["events"] else None
    return result


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions."""
    for service, handler, schema in (
        (SERVICE_REDIRECT_SHIPMENT, _async_redirect_shipment, REDIRECT_SHIPMENT_SCHEMA),
        (SERVICE_RESET_REDIRECTION, _async_reset_redirection, RESET_REDIRECTION_SCHEMA),
        (SERVICE_ADD_SHIPMENT, _async_add_shipment, ADD_REMOVE_SHIPMENT_SCHEMA),
        (SERVICE_REMOVE_SHIPMENT, _async_remove_shipment, ADD_REMOVE_SHIPMENT_SCHEMA),
        (SERVICE_RENAME_SHIPMENT, _async_rename_shipment, RENAME_SHIPMENT_SCHEMA),
    ):
        hass.services.async_register(DOMAIN, service, handler, schema=schema)
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_REDIRECTION_OPTIONS,
        _async_get_redirection_options,
        schema=SHIPMENT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_TRACK_SHIPMENT,
        _async_track_shipment,
        schema=TRACK_SHIPMENT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
