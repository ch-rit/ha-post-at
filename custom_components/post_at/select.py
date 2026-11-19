"""Selects to redirect incoming parcels from the UI."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from post_at import PostAtClient
from post_at.models import RedirectionState, RedirectionType, ShipmentRedirections

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, TARGET_PARCEL_LOCKER, TARGET_POST_OFFICE
from .coordinator import (
    REDIRECTION_TARGETS,
    PostAtConfigEntry,
    PostAtCoordinator,
    is_enabled,
    selected_redirection,
)
from .entity import PostAtShipmentEntity, async_setup_shipment_entities

PARALLEL_UPDATES = 1

OPTION_STANDARD = "standard"
OPTION_DROP_OFF_PLACE = "drop_off_place"
PLACE_OPTIONS = {
    "front_door": "Vor_Haustüre",
    "apartment_door": "Vor_Wohnungstüre",
    "mailbox": "AufOderUnter_Briefkasten",
    "garage": "In_Garage",
}
"""Selectable drop-off places -> place keys of the API (``sonstige`` needs a text)."""
READ_ONLY_OPTIONS = {
    RedirectionType.NEIGHBOR: "neighbor",
    RedirectionType.POST_OFFICE: "post_office",
    RedirectionType.PARCEL_LOCKER: "parcel_locker",
}
"""Redirections shown when active but set elsewhere (actions, app, pickup select)."""
DELIVERY_OPTIONS = [
    OPTION_STANDARD,
    *PLACE_OPTIONS,
    OPTION_DROP_OFF_PLACE,
    *READ_ONLY_OPTIONS.values(),
]

# The pickup options are branch names from the API, so they cannot be translated
# through translation keys.
PICKUP_LABELS = {
    "en": {
        TARGET_POST_OFFICE: ("Post office: {name}", "Nearest post office"),
        TARGET_PARCEL_LOCKER: ("Parcel locker: {name}", "Nearest parcel locker"),
    },
    "de": {
        TARGET_POST_OFFICE: ("Postfiliale: {name}", "Nächste Postfiliale"),
        TARGET_PARCEL_LOCKER: ("Abholstation: {name}", "Nächste Abholstation"),
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PostAtConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the selects of incoming, undelivered parcels."""
    coordinator = entry.runtime_data
    async_setup_shipment_entities(
        hass,
        entry,
        async_add_entities,
        coordinator.redirectable_shipments,
        {
            PostAtDeliveryOptionSelect.KEY: PostAtDeliveryOptionSelect,
            PostAtPickupLocationSelect.KEY: PostAtPickupLocationSelect,
        },
    )


class PostAtRedirectionSelect(PostAtShipmentEntity, SelectEntity):
    """A select changing the redirection of a parcel."""

    @property
    def _redirections(self) -> ShipmentRedirections | None:
        return self.coordinator.data.redirections.get(self.tracking_number)

    @property
    def available(self) -> bool:
        """Return whether the redirection options of the parcel are known."""
        return super().available and self._redirections is not None

    async def _async_redirect(self, func: Callable[[PostAtClient], Any]) -> None:
        await self.coordinator.async_run(func)
        await self.coordinator.async_request_refresh()


class PostAtDeliveryOptionSelect(PostAtRedirectionSelect, RestoreEntity):
    """Standard delivery or a drop-off place at the address."""

    KEY = "delivery_option"
    _attr_translation_key = "delivery_option"

    def __init__(self, coordinator: PostAtCoordinator, tracking_number: str) -> None:
        """Initialize the select."""
        super().__init__(coordinator, self.KEY, tracking_number)
        # The API does not return which place is set; remember the one chosen here.
        self._place_option: str | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the place chosen before a restart."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in PLACE_OPTIONS:
            self._place_option = last.state

    @property
    def _current_type(self) -> str | None:
        if (redirections := self._redirections) is None:
            return None
        current = next((o for o in redirections.options if o.current), None)
        return RedirectionType.DEFAULT if current is None else current.type

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._redirections is not None and self._current_type != RedirectionType.PLACE:
            self._place_option = None
        super()._handle_coordinator_update()

    @property
    def current_option(self) -> str | None:
        """Return the current delivery option."""
        current = self._current_type
        if current is None:
            return None
        if current == RedirectionType.DEFAULT:
            return OPTION_STANDARD
        if current == RedirectionType.PLACE:
            return self._place_option or OPTION_DROP_OFF_PLACE
        return READ_ONLY_OPTIONS.get(current)

    @property
    def options(self) -> list[str]:
        """Return standard, the places if possible, and the current option."""
        options = [OPTION_STANDARD]
        if is_enabled(self._redirections, RedirectionType.PLACE):
            options.extend(PLACE_OPTIONS)
        if (current := self.current_option) is not None and current not in options:
            options.append(current)
        return options

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return why redirections are not possible."""
        if (redirections := self._redirections) is None:
            return None
        return {
            "unavailability_reasons": {
                str(option.type): list(option.unavailability_reasons)
                for option in redirections.options
            },
            "redirect_possible": any(
                option.type != RedirectionType.DEFAULT
                and option.state == RedirectionState.ENABLED
                for option in redirections.options
            ),
        }

    async def async_select_option(self, option: str) -> None:
        """Redirect to a place or reset to standard delivery."""
        if option not in PLACE_OPTIONS and option != OPTION_STANDARD:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="option_not_selectable",
                translation_placeholders={"option": option},
            )
        if option == self.current_option:
            return
        number = self.tracking_number
        if option == OPTION_STANDARD:
            if (kind := REDIRECTION_TARGETS.get(self._current_type or "")) is None:
                return
            await self.coordinator.async_run(
                lambda client: client.reset_redirection(number, kind)
            )
            self._place_option = None
        else:
            place = PLACE_OPTIONS[option]
            await self.coordinator.async_run(
                lambda client: client.redirect_to_place(number, place)
            )
            self._place_option = option
        await self.coordinator.async_request_refresh()


class PostAtPickupLocationSelect(PostAtRedirectionSelect):
    """Redirect the parcel to a post office or parcel locker."""

    KEY = "pickup_location"
    _attr_translation_key = "pickup_location"

    def __init__(self, coordinator: PostAtCoordinator, tracking_number: str) -> None:
        """Initialize the select."""
        super().__init__(coordinator, self.KEY, tracking_number)

    @property
    def available(self) -> bool:
        """Return whether a post office or parcel locker can be chosen."""
        return super().available and (
            is_enabled(self._redirections, RedirectionType.POST_OFFICE)
            or is_enabled(self._redirections, RedirectionType.PARCEL_LOCKER)
        )

    def _label(self, target: str, branch_key: str | None) -> str:
        language = "de" if self.hass.config.language.startswith("de") else "en"
        template, nearest = PICKUP_LABELS[language][target]
        if branch_key is None:
            return nearest
        return template.format(
            name=self.coordinator.data.branch_names.get(branch_key, branch_key)
        )

    @property
    def _current(self) -> tuple[str, str | None] | None:
        if self._shipment is None or (selected := selected_redirection(self._shipment)) is None:
            return None
        return selected if selected[0] in (TARGET_POST_OFFICE, TARGET_PARCEL_LOCKER) else None

    @property
    def _choices(self) -> dict[str, tuple[str, str | None]]:
        """Return option label -> (target, branch key or None for the nearest)."""
        redirections = self._redirections
        targets = redirections.targets if redirections else None
        candidates: list[tuple[str, str | None]] = []
        for target, redirection_type, branches in (
            (
                TARGET_POST_OFFICE,
                RedirectionType.POST_OFFICE,
                targets.post_office_branches if targets else None,
            ),
            (
                TARGET_PARCEL_LOCKER,
                RedirectionType.PARCEL_LOCKER,
                targets.parcel_locker_branches if targets else None,
            ),
        ):
            if is_enabled(redirections, redirection_type):
                candidates.extend((target, key) for key in branches or [None])
        if (current := self._current) is not None and current not in candidates:
            candidates.append(current)
        choices: dict[str, tuple[str, str | None]] = {}
        for target, key in candidates:
            label = self._label(target, key)
            if label in choices:
                label = f"{label} ({key})"
            choices[label] = (target, key)
        return choices

    @property
    def options(self) -> list[str]:
        """Return the branches the parcel can be redirected to."""
        return list(self._choices)

    @property
    def current_option(self) -> str | None:
        """Return the branch the parcel is redirected to, if any."""
        current = self._current
        return next(
            (label for label, choice in self._choices.items() if choice == current), None
        )

    async def async_select_option(self, option: str) -> None:
        """Redirect the parcel to the chosen branch."""
        if option == self.current_option:
            return
        target, branch_key = self._choices[option]
        number = self.tracking_number
        if target == TARGET_POST_OFFICE:
            await self._async_redirect(
                lambda client: client.redirect_to_post_office(number, branch_key)
            )
        else:
            await self._async_redirect(
                lambda client: client.redirect_to_parcel_locker(number, branch_key)
            )
