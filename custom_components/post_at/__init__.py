"""The Österreichische Post integration."""

from __future__ import annotations

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import DATA_LOCKS, PostAtConfigEntry, PostAtCoordinator
from .entity import async_setup_parcel_devices
from .services import async_setup_services

PLATFORMS: list[Platform] = [Platform.SELECT, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the actions."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PostAtConfigEntry) -> bool:
    """Set up a Post account from a config entry."""
    coordinator = PostAtCoordinator(hass, entry)
    # Also runs when the first refresh fails, closing the client again.
    entry.async_on_unload(coordinator.async_close)

    async def _async_close_on_stop(event: Event) -> None:
        # Entries are not unloaded on shutdown; wait for a running call so its
        # token rotation is stored before the final write.
        await coordinator.async_close()

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _async_close_on_stop)
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # After the platforms, so entities are removed before their parcel device.
    async_setup_parcel_devices(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PostAtConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: PostAtConfigEntry) -> None:
    """Forget the client lock of a removed entry."""
    hass.data.get(DATA_LOCKS, {}).pop(entry.entry_id, None)
