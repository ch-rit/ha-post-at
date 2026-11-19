"""Diagnostics for the Österreichische Post integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_SESSION
from .coordinator import PostAtConfigEntry

TO_REDACT_ENTRY = {CONF_SESSION, "title", "unique_id"}
# Keys of the snake_case shipment dump that hold personal data. ``place`` covers
# both event locations and the place redirection with its free-text description.
TO_REDACT_DATA = {
    "tracking_number",
    "original_tracking_number",
    "title",
    "sender",
    "preshipper",
    "recipient",
    "recipient_address",
    "delivery_address_text",
    "notification_identifier",
    "sso_user_id_list",
    "image",
    "parcel_visualization",
    "parcel_stamp_id",
    "branch_key",
    "postal_code",
    "place",
    "first_name",
    "last_name",
    "street",
    "street_number",
    "selected_branch",
    "selected_redirection_area_code",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PostAtConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT_ENTRY),
        "last_update_success": coordinator.last_update_success,
        "data": async_redact_data(
            {
                "shipments": data.shipments.model_dump(mode="json", by_alias=False),
                # A list: the dict is keyed by tracking number.
                "redirections": [
                    redirections.model_dump(mode="json", by_alias=False)
                    for redirections in data.redirections.values()
                ],
                "branch_names": data.branch_names,
                "places": [place.model_dump(mode="json") for place in data.places],
            },
            TO_REDACT_DATA,
        ),
    }
