"""Sensors for the Österreichische Post integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from post_at.models import Shipment

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DIRECTION_INCOMING, DIRECTION_OUTGOING, DOMAIN, SHIPMENT_STATES
from .coordinator import (
    PostAtConfigEntry,
    PostAtCoordinator,
    current_redirection,
    is_delivered,
    is_ready_for_pickup,
    shipment_direction,
    shipment_state,
)

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

    prefix = PostAtShipmentSensor.unique_id_prefix(coordinator)
    added: set[str] = set()

    @callback
    def _async_sync_shipments() -> None:
        visible = coordinator.visible_shipments()
        if new := [number for number in visible if number not in added]:
            added.update(new)
            async_add_entities(PostAtShipmentSensor(coordinator, number) for number in new)
        registry = er.async_get(hass)
        for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            if registry_entry.domain != SENSOR_DOMAIN or not (
                registry_entry.unique_id.startswith(prefix)
            ):
                continue
            number = registry_entry.unique_id.removeprefix(prefix)
            if number not in visible:
                added.discard(number)
                registry.async_remove(registry_entry.entity_id)

    _async_sync_shipments()
    entry.async_on_unload(coordinator.async_add_listener(_async_sync_shipments))


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


class PostAtEntity(CoordinatorEntity[PostAtCoordinator]):
    """An entity of a Post account."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PostAtCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.account_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.account_id)},
            name=coordinator.config_entry.title,
            manufacturer="Österreichische Post AG",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.post.at/",
        )


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
            s for s in self.coordinator.data.shipments if self.entity_description.filter_fn(s)
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


class PostAtShipmentSensor(PostAtEntity, SensorEntity):
    """Tracking state of one shipment."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SHIPMENT_STATES
    _attr_translation_key = "shipment"

    def __init__(self, coordinator: PostAtCoordinator, tracking_number: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, f"shipment_{tracking_number}")
        self.tracking_number = tracking_number
        self._shipment = coordinator.shipment(tracking_number)

    @staticmethod
    def unique_id_prefix(coordinator: PostAtCoordinator) -> str:
        """Return the unique id prefix of the per-shipment sensors."""
        return f"{coordinator.account_id}_shipment_"

    @callback
    def _handle_coordinator_update(self) -> None:
        self._shipment = self.coordinator.shipment(self.tracking_number)
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return whether the shipment is still in the account."""
        return super().available and self._shipment is not None

    @property
    def name(self) -> str:
        """Return the shipment's title, else its sender, else its tracking number."""
        shipment = self._shipment
        return (shipment and (shipment.title or shipment.sender)) or self.tracking_number

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
