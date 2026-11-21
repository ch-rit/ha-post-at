"""Images for the Österreichische Post integration."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import PostAtConfigEntry, PostAtCoordinator
from .entity import PostAtShipmentEntity, async_setup_shipment_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PostAtConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Keep a photo entity for each shipment the Post took a photo of."""
    coordinator = entry.runtime_data
    async_setup_shipment_entities(
        hass,
        entry,
        async_add_entities,
        lambda: [
            number
            for number, shipment in coordinator.visible_shipments().items()
            if shipment.image_url
        ],
        {PostAtShipmentPhoto.KEY: PostAtShipmentPhoto},
    )


class PostAtShipmentPhoto(PostAtShipmentEntity, ImageEntity):
    """Photo of the parcel from the sorting conveyor: the "Photo" image of the parcel device."""

    KEY = "photo"
    _attr_translation_key = "photo"

    def __init__(self, coordinator: PostAtCoordinator, tracking_number: str) -> None:
        """Initialize the image."""
        super().__init__(coordinator, self.KEY, tracking_number)
        ImageEntity.__init__(self, coordinator.hass)
        self._update_photo()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._shipment = self.coordinator.shipment(self.tracking_number)
        self._update_photo()
        super()._handle_coordinator_update()

    def _update_photo(self) -> None:
        """Point at the shipment's photo; a missing one keeps the last photo."""
        if self._shipment is None or (url := self._shipment.image_url) is None:
            return
        if url == self._attr_image_url:
            return
        self._attr_image_url = url
        self._attr_image_last_updated = dt_util.utcnow()
        self._cached_image = None
