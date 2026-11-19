"""Base entities of the Österreichische Post integration."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PostAtConfigEntry, PostAtCoordinator, shipment_name

MANUFACTURER = "Österreichische Post AG"


def parcel_device_identifier(entry: PostAtConfigEntry, tracking_number: str) -> str:
    """Return the device identifier of a parcel."""
    return f"{entry.entry_id}_{tracking_number}"


class PostAtEntity(CoordinatorEntity[PostAtCoordinator]):
    """An entity of a Post account, on the account device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PostAtCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.account_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.account_id)},
            name=coordinator.config_entry.title,
            manufacturer=MANUFACTURER,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.post.at/",
        )


class PostAtShipmentEntity(PostAtEntity):
    """An entity of one parcel, on the parcel's device."""

    def __init__(
        self, coordinator: PostAtCoordinator, key: str, tracking_number: str
    ) -> None:
        """Initialize the entity; the unique id is ``<account>_<key>_<number>``."""
        super().__init__(coordinator, f"{key}_{tracking_number}")
        self.tracking_number = tracking_number
        self._shipment = shipment = coordinator.shipment(tracking_number)
        self._attr_device_info = DeviceInfo(
            identifiers={
                (DOMAIN, parcel_device_identifier(coordinator.config_entry, tracking_number))
            },
            name=shipment_name(shipment, tracking_number),
            model=shipment.product_category if shipment else None,
            manufacturer=MANUFACTURER,
            entry_type=DeviceEntryType.SERVICE,
            via_device=(DOMAIN, coordinator.account_id),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._shipment = self.coordinator.shipment(self.tracking_number)
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return whether the shipment is still in the account."""
        return super().available and self._shipment is not None


@callback
def async_setup_shipment_entities(
    hass: HomeAssistant,
    entry: PostAtConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    tracking_numbers: Callable[[], list[str]],
    entity_types: Mapping[str, Callable[[PostAtCoordinator, str], Entity]],
) -> None:
    """Keep one entity per key of ``entity_types`` for each wanted tracking number.

    Entities of shipments no longer wanted are removed from the entity registry,
    also those left over from before a restart.
    """
    coordinator = entry.runtime_data
    prefixes = {key: f"{coordinator.account_id}_{key}_" for key in entity_types}
    added: set[tuple[str, str]] = set()

    @callback
    def _async_sync() -> None:
        wanted = tracking_numbers()
        if new := [
            (key, number)
            for number in wanted
            for key in entity_types
            if (key, number) not in added
        ]:
            added.update(new)
            async_add_entities(entity_types[key](coordinator, number) for key, number in new)
        registry = er.async_get(hass)
        for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            for key, prefix in prefixes.items():
                if not registry_entry.unique_id.startswith(prefix):
                    continue
                number = registry_entry.unique_id.removeprefix(prefix)
                if number not in wanted:
                    added.discard((key, number))
                    registry.async_remove(registry_entry.entity_id)
                break

    _async_sync()
    entry.async_on_unload(coordinator.async_add_listener(_async_sync))


@callback
def async_setup_parcel_devices(hass: HomeAssistant, entry: PostAtConfigEntry) -> None:
    """Rename parcel devices with their shipment and remove those that dropped out."""
    coordinator = entry.runtime_data
    prefix = parcel_device_identifier(entry, "")

    @callback
    def _async_sync() -> None:
        visible = coordinator.visible_shipments()
        registry = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            number = next(
                (
                    identifier.removeprefix(prefix)
                    for domain, identifier in device.identifiers
                    if domain == DOMAIN and identifier.startswith(prefix)
                ),
                None,
            )
            if number is None:
                continue
            if (shipment := visible.get(number)) is None:
                registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
            elif device.name != (name := shipment_name(shipment, number)):
                registry.async_update_device(device.id, name=name)

    _async_sync()
    entry.async_on_unload(coordinator.async_add_listener(_async_sync))
