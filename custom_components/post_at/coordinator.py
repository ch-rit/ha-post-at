"""Data update coordinator for the Österreichische Post integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
import re
from typing import Any, NamedTuple

import httpx
from post_at import (
    ApiError,
    AuthenticationError,
    GraphQLError,
    PostAtClient,
    PostAtError,
    Session,
)
from post_at.models import (
    Place,
    RedirectionOption,
    RedirectionState,
    RedirectionType,
    Shipment,
    ShipmentList,
    ShipmentRedirections,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import (
    CONF_KEEP_DELIVERED_DAYS,
    CONF_SESSION,
    DEFAULT_KEEP_DELIVERED_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    EVENT_SHIPMENT_UPDATED,
    LOGGER,
    PICKUP_CLAIMS,
    SHIPMENT_STATES,
    STATE_DELIVERED,
    STATE_UNKNOWN,
    TARGET_NEIGHBOR,
    TARGET_PARCEL_LOCKER,
    TARGET_PLACE,
    TARGET_POST_OFFICE,
)

type PostAtConfigEntry = ConfigEntry[PostAtCoordinator]

DATA_LOCKS: HassKey[dict[str, asyncio.Lock]] = HassKey(f"{DOMAIN}_client_locks")
"""Per config entry lock around all client use; kept across reloads."""

REDIRECTION_TARGETS: dict[str, str] = {
    RedirectionType.PLACE: TARGET_PLACE,
    RedirectionType.NEIGHBOR: TARGET_NEIGHBOR,
    RedirectionType.POST_OFFICE: TARGET_POST_OFFICE,
    RedirectionType.PARCEL_LOCKER: TARGET_PARCEL_LOCKER,
}
"""Redirection type of the API -> redirection target of the library and actions."""

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True, slots=True)
class PostAtData:
    """Data of one account."""

    shipments: ShipmentList
    redirections: dict[str, ShipmentRedirections]
    """Redirection options of incoming, undelivered shipments by tracking number."""
    branch_names: dict[str, str]
    """Names of the branches offered as redirection targets, by branch key."""
    places: list[Place]


class _Fetched(NamedTuple):
    shipments: ShipmentList
    redirections: dict[str, ShipmentRedirections]
    places: list[Place] | None
    branch_names: dict[str, str]


def state_key(shipment: Shipment) -> str | None:
    """Return the library's raw ``state_key`` of the latest event."""
    return event.state_key if (event := shipment.latest_event) else None


def shipment_state(shipment: Shipment) -> str:
    """Return the snake_case tracking state, ``unknown`` for keys we do not know."""
    if (key := state_key(shipment)) is None:
        return STATE_UNKNOWN
    state = _CAMEL_BOUNDARY.sub("_", key).lower()
    return state if state in SHIPMENT_STATES else STATE_UNKNOWN


def is_delivered(shipment: Shipment) -> bool:
    """Return whether the shipment is delivered."""
    return shipment_state(shipment) == STATE_DELIVERED


def shipment_direction(shipment: Shipment) -> str:
    """Return ``incoming`` or ``outgoing``."""
    if shipment.is_recipient is not None:
        return DIRECTION_INCOMING if shipment.is_recipient else DIRECTION_OUTGOING
    if any(tag.key == "versenden" for tag in shipment.tags):
        return DIRECTION_OUTGOING
    return DIRECTION_INCOMING


def is_redirectable(shipment: Shipment) -> bool:
    """Return whether the shipment is incoming and not delivered yet."""
    return shipment_direction(shipment) == DIRECTION_INCOMING and not is_delivered(shipment)


def shipment_name(shipment: Shipment | None, tracking_number: str) -> str:
    """Return the shipment's title, else its sender, else its tracking number."""
    return (shipment and (shipment.title or shipment.sender)) or tracking_number


def is_ready_for_pickup(shipment: Shipment) -> bool:
    """Return whether the shipment waits at a post office or parcel locker."""
    notification = shipment.notification
    return (
        notification is not None
        and notification.claim in PICKUP_CLAIMS
        and not is_delivered(shipment)
    )


def current_redirection(shipment: Shipment) -> str | None:
    """Return the redirection target currently set for the shipment, if any."""
    selected = selected_redirection(shipment)
    return selected[0] if selected else None


def selected_redirection(shipment: Shipment) -> tuple[str, str | None] | None:
    """Return the selected redirection target and, for branches, the branch key."""
    for redirection in shipment.redirections:
        if (place := redirection.place) and place.is_selected:
            return TARGET_PLACE, None
        if (neighbor := redirection.neighbor) and neighbor.is_selected:
            return TARGET_NEIGHBOR, None
        if (office := redirection.post_office) and office.is_selected:
            return TARGET_POST_OFFICE, office.selected_branch
        if (locker := redirection.parcel_locker) and locker.is_selected:
            return TARGET_PARCEL_LOCKER, locker.selected_branch
    return None


def redirection_option(
    redirections: ShipmentRedirections | None, redirection_type: RedirectionType
) -> RedirectionOption | None:
    """Return the option of one redirection type."""
    if redirections is None:
        return None
    return next((o for o in redirections.options if o.type == redirection_type), None)


def is_enabled(
    redirections: ShipmentRedirections | None, redirection_type: RedirectionType
) -> bool:
    """Return whether a redirection type can be chosen for the shipment."""
    option = redirection_option(redirections, redirection_type)
    return option is not None and option.state == RedirectionState.ENABLED


def library_error(err: Exception) -> HomeAssistantError:
    """Map a library or transport error to a translated error."""
    if isinstance(err, AuthenticationError):
        return HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed")
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=(
            "communication_error" if isinstance(err, httpx.HTTPError) else "api_error"
        ),
        translation_placeholders={"error": str(err)},
    )


def _fetch(
    client: PostAtClient, fetch_places: bool, known_branches: frozenset[str]
) -> _Fetched:
    """Fetch shipments, redirection options, places and new branch names."""
    shipments = client.shipments()
    redirections: dict[str, ShipmentRedirections] = {}
    for shipment in shipments.shipments:
        if not is_redirectable(shipment):
            continue
        number = shipment.tracking_number
        try:
            result = client.possible_redirections(number)
        except (ApiError, GraphQLError) as err:
            LOGGER.debug("Could not fetch the redirection options of %s: %s", number, err)
            continue
        if result is not None:
            redirections[number] = result

    places = None
    if fetch_places:
        try:
            places = client.places()
        except (ApiError, GraphQLError) as err:
            LOGGER.debug("Could not fetch the drop-off places: %s", err)

    unknown = {
        key
        for result in redirections.values()
        if result.targets
        for key in (result.targets.post_office_branches or [])
        + (result.targets.parcel_locker_branches or [])
    } - known_branches
    branch_names: dict[str, str] = {}
    if unknown:
        try:
            branches = client.branches(branch_keys=sorted(unknown))
        except (ApiError, GraphQLError) as err:
            LOGGER.debug("Could not fetch the names of branches %s: %s", unknown, err)
        else:
            branch_names = {b.branch_key: b.name for b in branches if b.name}
    return _Fetched(shipments, redirections, places, branch_names)


class PostAtCoordinator(DataUpdateCoordinator[PostAtData]):
    """Fetches the account's shipments and owns the (synchronous) library client.

    All client calls run in the executor, one at a time: the library's
    authenticator is not thread-safe and B2C refresh tokens are single-use, so two
    concurrent token refreshes would log the account out. The lock is per entry and
    outlives reloads, so a new client only reads the stored session once a call of
    the previous client, and the rotation it stores, has finished.
    """

    config_entry: PostAtConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PostAtConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.account_id = entry.unique_id or entry.entry_id
        self._client: PostAtClient | None = None
        self._lock = hass.data.setdefault(DATA_LOCKS, {}).setdefault(
            entry.entry_id, asyncio.Lock()
        )
        # Refresh token this client last loaded or stored; see _async_store_session.
        self._refresh_token: str | None = None
        # Fetched once; branch names are only fetched for keys not seen before.
        self._places: list[Place] | None = None
        self._branch_names: dict[str, str] = {}

    async def _async_setup(self) -> None:
        """Create the client; httpx loads its certificates, which blocks."""
        async with self._lock:
            session = self.config_entry.data[CONF_SESSION]
            self._refresh_token = session["refresh_token"]
            self._client = await self.hass.async_add_executor_job(
                partial(
                    PostAtClient,
                    session=Session.from_dict(session),
                    on_session_change=self._on_session_change,
                )
            )

    async def async_close(self) -> None:
        """Wait for a running client call, then close the client."""
        async with self._lock:
            if (client := self._client) is not None:
                self._client = None
                await self.hass.async_add_executor_job(client.close)

    async def async_call[T](self, func: Callable[[PostAtClient], T]) -> T:
        """Run ``func(client)`` in the executor, serialized with all other calls.

        Cancelling the caller (unload, shutdown) does not release the lock before
        the thread is done: it may be in the middle of a token rotation, which must
        be stored before anyone closes the client or reads the session again.
        """
        async with self._lock:
            if self._client is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="entry_not_loaded",
                    translation_placeholders={"title": self.config_entry.title},
                )
            job = self.hass.async_add_executor_job(func, self._client)
            try:
                return await asyncio.shield(job)
            except asyncio.CancelledError:
                while not job.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait([job])
                raise

    async def async_run[T](self, func: Callable[[PostAtClient], T]) -> T:
        """Like async_call, raising library errors as translated errors."""
        try:
            return await self.async_call(func)
        except (PostAtError, httpx.HTTPError) as err:
            raise library_error(err) from err

    def _on_session_change(self, session: Session | None) -> None:
        """Persist rotated tokens; runs in the executor thread of the request.

        The session object is mutated in place by later refreshes, so it is
        serialized here, on the calling thread, before handing it to the event
        loop. ``call_soon_threadsafe`` is FIFO, so the entry is updated before the
        executor job's completion reaches the loop, i.e. before async_call returns
        or releases the lock.
        """
        if session is None:
            return
        self.hass.loop.call_soon_threadsafe(self._async_store_session, session.to_dict())

    @callback
    def _async_store_session(self, session: dict[str, Any]) -> None:
        """Store a rotated session in the config entry."""
        entry = self.config_entry
        if entry.data[CONF_SESSION]["refresh_token"] != self._refresh_token:
            # Re-authentication replaced the session; this client's token chain is stale.
            return
        self._refresh_token = session["refresh_token"]
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_SESSION: session}
        )

    async def _async_update_data(self) -> PostAtData:
        """Fetch the shipment list and the redirection options."""
        fetch_places = self._places is None
        known_branches = frozenset(self._branch_names)
        try:
            fetched = await self.async_call(
                lambda client: _fetch(client, fetch_places, known_branches)
            )
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except (PostAtError, httpx.HTTPError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if fetched.places is not None:
            self._places = fetched.places
        if fetched.branch_names:
            self._branch_names = {**self._branch_names, **fetched.branch_names}
        if self.data is not None:
            self._async_fire_state_changes(self.data.shipments, fetched.shipments)
        return PostAtData(
            shipments=fetched.shipments,
            redirections=fetched.redirections,
            branch_names=self._branch_names,
            places=self._places or [],
        )

    @callback
    def _async_fire_state_changes(self, old: ShipmentList, new: ShipmentList) -> None:
        """Fire an event for every known shipment whose tracking state changed."""
        previous = {shipment.tracking_number: shipment for shipment in old.shipments}
        for shipment in new.shipments:
            before = previous.get(shipment.tracking_number)
            if before is None or state_key(before) == state_key(shipment):
                continue
            event = shipment.latest_event
            self.hass.bus.async_fire(
                EVENT_SHIPMENT_UPDATED,
                {
                    "tracking_number": shipment.tracking_number,
                    "title": shipment.title,
                    "sender": shipment.sender,
                    "old_state": shipment_state(before),
                    "new_state": shipment_state(shipment),
                    "state_text": event.state if event else None,
                    "direction": shipment_direction(shipment),
                },
            )

    def shipment(self, tracking_number: str) -> Shipment | None:
        """Return a shipment of the current data by tracking number."""
        return next(
            (
                s
                for s in self.data.shipments.shipments
                if s.tracking_number == tracking_number
            ),
            None,
        )

    def visible_shipments(self) -> dict[str, Shipment]:
        """Return shipments that get a device: not delivered or delivered recently."""
        days = self.config_entry.options.get(
            CONF_KEEP_DELIVERED_DAYS, DEFAULT_KEEP_DELIVERED_DAYS
        )
        cutoff = dt_util.utcnow() - timedelta(days=days)
        return {
            shipment.tracking_number: shipment
            for shipment in self.data.shipments.shipments
            if (event := shipment.latest_event) is None
            or not is_delivered(shipment)
            or event.timestamp >= cutoff
        }

    def redirectable_shipments(self) -> list[str]:
        """Return the tracking numbers of incoming, undelivered shipments."""
        return [
            shipment.tracking_number
            for shipment in self.data.shipments.shipments
            if is_redirectable(shipment)
        ]
