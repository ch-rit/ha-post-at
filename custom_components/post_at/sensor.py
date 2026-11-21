"""Sensors for the Österreichische Post integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from post_at.models import Shipment, ShipmentEvent

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DIRECTION_INCOMING, DIRECTION_OUTGOING, SHIPMENT_STATES
from .coordinator import (
    PostAtConfigEntry,
    PostAtCoordinator,
    current_redirection,
    is_delivered,
    is_ready_for_pickup,
    shipment_direction,
    shipment_state,
)
from .entity import PostAtEntity, PostAtShipmentEntity, async_setup_shipment_entities

PARALLEL_UPDATES = 0

ATTR_SHIPMENTS = "shipments"


@dataclass(frozen=True, kw_only=True)
class PostAtSummarySensorDescription(SensorEntityDescription):
    """Describes a sensor counting shipments."""

    filter_fn: Callable[[Shipment], bool]
    list_attribute: bool = False


SUMMARY_SENSORS: tuple[PostAtSummarySensorDescription, ...] = (
    PostAtSummarySensorDescription(
        key="incoming",
        translation_key="incoming",
        state_class=SensorStateClass.MEASUREMENT,
        filter_fn=lambda s: (
            shipment_direction(s) == DIRECTION_INCOMING and not is_delivered(s)
        ),
        list_attribute=True,
    ),
    PostAtSummarySensorDescription(
        key="outgoing",
        translation_key="outgoing",
        state_class=SensorStateClass.MEASUREMENT,
        filter_fn=lambda s: (
            shipment_direction(s) == DIRECTION_OUTGOING and not is_delivered(s)
        ),
        list_attribute=True,
    ),
    PostAtSummarySensorDescription(
        key="ready_for_pickup",
        translation_key="ready_for_pickup",
        state_class=SensorStateClass.MEASUREMENT,
        filter_fn=is_ready_for_pickup,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PostAtConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors and keep the per-shipment sensors in sync."""
    coordinator = entry.runtime_data
    async_add_entities(
        PostAtSummarySensor(coordinator, description) for description in SUMMARY_SENSORS
    )
    async_setup_shipment_entities(
        hass,
        entry,
        async_add_entities,
        lambda: list(coordinator.visible_shipments()),
        {
            PostAtShipmentSensor.KEY: PostAtShipmentSensor,
            PostAtShipmentLocationSensor.KEY: PostAtShipmentLocationSensor,
        },
    )


def _shipment_summary(shipment: Shipment) -> dict[str, Any]:
    event = shipment.latest_event
    return {
        "tracking_number": shipment.tracking_number,
        "title": shipment.title,
        "sender": shipment.sender,
        "state": shipment_state(shipment),
        "state_text": event.state if event else None,
        "last_update": event.timestamp.isoformat() if event else None,
        "estimated_delivery": shipment.estimated_delivery_date_text,
    }


def _located_event(shipment: Shipment) -> ShipmentEvent | None:
    """Return the latest event with a place or postal code; announcements have neither."""
    return next((e for e in reversed(shipment.events) if e.place or e.postal_code), None)


class PostAtSummarySensor(PostAtEntity, SensorEntity):
    """Counts shipments of the account."""

    entity_description: PostAtSummarySensorDescription
    _unrecorded_attributes = frozenset({ATTR_SHIPMENTS})

    def __init__(
        self,
        coordinator: PostAtCoordinator,
        description: PostAtSummarySensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def _shipments(self) -> list[Shipment]:
        return [
            s
            for s in self.coordinator.data.shipments.shipments
            if self.entity_description.filter_fn(s)
        ]

    @property
    def native_value(self) -> int:
        """Return the number of matching shipments."""
        return len(self._shipments)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the matching shipments."""
        if not self.entity_description.list_attribute:
            return None
        return {ATTR_SHIPMENTS: [_shipment_summary(s) for s in self._shipments]}


class PostAtShipmentSensor(PostAtShipmentEntity, SensorEntity):
    """Tracking state of one shipment: the "Status" sensor of the parcel device."""

    KEY = "shipment"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SHIPMENT_STATES
    _attr_translation_key = "shipment"

    def __init__(self, coordinator: PostAtCoordinator, tracking_number: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, self.KEY, tracking_number)

    @property
    def native_value(self) -> str | None:
        """Return the tracking state."""
        return shipment_state(self._shipment) if self._shipment else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return details of the shipment."""
        if (shipment := self._shipment) is None:
            return None
        event = shipment.latest_event
        return {
            "tracking_number": shipment.tracking_number,
            "sender": shipment.sender,
            "title": shipment.title,
            "direction": shipment_direction(shipment),
            "state_key": event.state_key if event else None,
            "state_text": event.state if event else None,
            "last_event_description": event.description if event else None,
            "last_event_timestamp": event.timestamp.isoformat() if event else None,
            "estimated_delivery": shipment.estimated_delivery_date_text,
            "delivery_type": (
                str(shipment.delivery_type) if shipment.delivery_type is not None else None
            ),
            "weight": shipment.weight,
            "redirection": current_redirection(shipment),
        }


class PostAtShipmentLocationSensor(PostAtShipmentEntity, SensorEntity):
    """Where the shipment was last scanned: the "Location" sensor of the parcel device."""

    KEY = "location"
    _attr_translation_key = "location"

    def __init__(self, coordinator: PostAtCoordinator, tracking_number: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, self.KEY, tracking_number)

    @property
    def native_value(self) -> str | None:
        """Return the place name, else the postal code, of the last located event."""
        if self._shipment is None or (event := _located_event(self._shipment)) is None:
            return None
        return event.place or event.postal_code

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the postal code, country and time of the last located event."""
        if self._shipment is None or (event := _located_event(self._shipment)) is None:
            return None
        return {
            "postal_code": event.postal_code or None,
            "country": event.country,
            "timestamp": event.timestamp.isoformat(),
        }
