"""Tests for setup, the coordinator and session persistence."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import threading
import time
from typing import Any
from unittest.mock import DEFAULT, MagicMock

from freezegun.api import FrozenDateTimeFactory
import httpx
from post_at import ApiError, AuthenticationError, GraphQLError
import pytest

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.post_at.const import CONF_SESSION, DOMAIN, EVENT_SHIPMENT_UPDATED
from custom_components.post_at.coordinator import PostAtCoordinator

from .conftest import entity_id, make_session, shipment_data, shipment_list


async def test_setup_and_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client_class: MagicMock
) -> None:
    """The entry loads with the stored session and closes the client on unload."""
    assert init_integration.state is ConfigEntryState.LOADED
    kwargs = mock_client_class.call_args.kwargs
    assert kwargs["session"].to_dict() == init_integration.data[CONF_SESSION]
    assert callable(kwargs["on_session_change"])

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.NOT_LOADED
    mock_client_class.return_value.close.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        ApiError(500, "error", None),
        GraphQLError([{"message": "boom"}], None),
        httpx.ConnectError("unreachable"),
    ],
)
async def test_setup_retry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    error: Exception,
) -> None:
    """Errors of the first update retry the setup and close the client."""
    mock_client.shipments.side_effect = error
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    mock_client.close.assert_called_once()


async def test_setup_auth_failed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """An expired session at setup starts a re-authentication."""
    mock_client.shipments.side_effect = AuthenticationError("invalid_grant")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_update_auth_failed_starts_reauth(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An authentication error during polling starts a re-authentication."""
    mock_client.shipments.side_effect = AuthenticationError("invalid_grant")
    freezer.tick(timedelta(minutes=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert flows[0]["context"]["entry_id"] == init_integration.entry_id
    assert hass.states.get(entity_id(hass, "incoming")).state == STATE_UNAVAILABLE


async def test_update_failed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Other errors make the entities unavailable until the next update."""
    mock_client.shipments.side_effect = ApiError(502, "bad gateway", "op")
    freezer.tick(timedelta(minutes=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "incoming")).state == STATE_UNAVAILABLE
    assert not hass.config_entries.flow.async_progress()

    mock_client.shipments.side_effect = None
    freezer.tick(timedelta(minutes=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "incoming")).state == "3"


async def test_session_rotation_persisted(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client_class: MagicMock,
    mock_client: MagicMock,
) -> None:
    """Tokens rotated in the executor thread are stored in the entry."""
    on_session_change = mock_client_class.call_args.kwargs["on_session_change"]
    loop_thread = threading.get_ident()
    threads: list[int] = []

    def rotate(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        session = make_session("refresh-2")
        on_session_change(session)
        # The library keeps mutating the same object on later refreshes.
        session.refresh_token = "mutated-later"
        on_session_change(None)
        return DEFAULT

    mock_client.shipments.side_effect = rotate
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert threads and loop_thread not in threads
    assert init_integration.data[CONF_SESSION] == make_session("refresh-2").to_dict()

    # A later rotation of the same chain is stored too.
    mock_client.shipments.side_effect = lambda *a, **k: (
        on_session_change(make_session("refresh-3")),
        DEFAULT,
    )[1]
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert init_integration.data[CONF_SESSION]["refresh_token"] == "refresh-3"


async def test_stale_rotation_ignored_after_reauth(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client_class: MagicMock,
    mock_client: MagicMock,
) -> None:
    """A rotation of the old client must not overwrite a re-authenticated session."""
    on_session_change = mock_client_class.call_args.kwargs["on_session_change"]
    hass.config_entries.async_update_entry(
        init_integration, data={CONF_SESSION: make_session("reauth").to_dict()}
    )
    mock_client.shipments.side_effect = lambda *a, **k: (
        on_session_change(make_session("old-chain")),
        DEFAULT,
    )[1]
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert init_integration.data[CONF_SESSION]["refresh_token"] == "reauth"


async def test_client_calls_serialized(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The client is never used from two threads at once."""
    lock = threading.Lock()
    active = 0
    peak = 0

    def slow(*args: Any, **kwargs: Any) -> Any:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return DEFAULT

    mock_client.shipments.side_effect = slow
    mock_client.rename_shipment.side_effect = slow
    mock_client.add_shipment.side_effect = slow
    await asyncio.gather(
        init_integration.runtime_data.async_refresh(),
        hass.services.async_call(
            DOMAIN, "rename_shipment", {"tracking_number": "1001", "title": "x"}, blocking=True
        ),
        hass.services.async_call(DOMAIN, "add_shipment", {"tracking_number": "9"}, blocking=True),
    )
    await hass.async_block_till_done()
    assert peak == 1
    assert mock_client.rename_shipment.called
    assert mock_client.add_shipment.called


def _block_mid_rotation(
    client_class: MagicMock, client: MagicMock, token: str
) -> tuple[threading.Event, threading.Event]:
    """Make the next shipments() call block while rotating the refresh token."""
    on_session_change = client_class.call_args.kwargs["on_session_change"]
    started, release = threading.Event(), threading.Event()

    def rotate(*args: Any, **kwargs: Any) -> Any:
        started.set()
        assert release.wait(5)
        on_session_change(make_session(token))
        return DEFAULT

    client.shipments.side_effect = rotate
    return started, release


async def test_unload_during_rotation_in_background_refresh(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client_class: MagicMock,
    mock_client: MagicMock,
) -> None:
    """Unloading cancels the polling task; the rotation in flight is still stored.

    The client is only closed once the executor job ends, and the reloaded
    coordinator starts with the rotated token.
    """
    started, release = _block_mid_rotation(mock_client_class, mock_client, "rotated")
    # The coordinator polls in a config entry background task, which unload cancels.
    refresh = init_integration.async_create_background_task(
        hass, init_integration.runtime_data.async_refresh(), "post_at refresh"
    )
    await hass.async_add_executor_job(started.wait)

    unload = hass.async_create_task(hass.config_entries.async_unload(init_integration.entry_id))
    await asyncio.sleep(0.05)
    assert refresh.cancelling() or refresh.cancelled()
    assert not unload.done()
    mock_client.close.assert_not_called()

    release.set()
    assert await unload
    assert refresh.cancelled()
    mock_client.close.assert_called_once()
    assert init_integration.data[CONF_SESSION]["refresh_token"] == "rotated"

    mock_client.shipments.side_effect = None
    assert await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()
    assert mock_client_class.call_args.kwargs["session"].refresh_token == "rotated"


async def test_setup_waits_for_previous_client(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client_class: MagicMock,
    mock_client: MagicMock,
) -> None:
    """A new coordinator reads the session only after the old client's call ended.

    Covers Home Assistant giving up waiting for unload tasks after 10 seconds.
    """
    old = init_integration.runtime_data
    started, release = _block_mid_rotation(mock_client_class, mock_client, "rotated")
    refresh = hass.async_create_task(old.async_refresh())
    await hass.async_add_executor_job(started.wait)

    # A second coordinator for the same entry, as after a reload.
    new = PostAtCoordinator(hass, init_integration)
    setup = hass.async_create_task(new._async_setup())
    await asyncio.sleep(0.05)
    assert not setup.done()
    assert mock_client_class.call_count == 1

    release.set()
    await refresh
    await setup
    assert mock_client_class.call_count == 2
    assert mock_client_class.call_args.kwargs["session"].refresh_token == "rotated"
    await new.async_close()


async def test_shutdown_waits_for_rotation(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client_class: MagicMock,
    mock_client: MagicMock,
) -> None:
    """On shutdown the client is closed only after a running rotation was stored."""
    started, release = _block_mid_rotation(mock_client_class, mock_client, "rotated")
    refresh = init_integration.async_create_background_task(
        hass, init_integration.runtime_data.async_refresh(), "post_at refresh"
    )
    await hass.async_add_executor_job(started.wait)
    refresh.cancel()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await asyncio.sleep(0.05)
    mock_client.close.assert_not_called()

    release.set()
    await hass.async_block_till_done()
    mock_client.close.assert_called_once()
    assert init_integration.data[CONF_SESSION]["refresh_token"] == "rotated"


async def test_shipment_updated_event(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """State changes fire an event; the first update and new shipments do not."""
    events = async_capture_events(hass, EVENT_SHIPMENT_UPDATED)
    coordinator = init_integration.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert events == []

    mock_client.shipments.return_value = shipment_list(
        shipment_data("1001", "aviso", "deliveryHandOver", "inDelivery", title="Headphones"),
        shipment_data("1002", "aviso", "deliveryHandOver"),
        shipment_data("1003", "inDistribution", "somethingNew", title="Gift", incoming=False),
        shipment_data("1007", "aviso"),
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert [event.data for event in events] == [
        {
            "tracking_number": "1002",
            "title": "",
            "sender": "Shop GmbH",
            "old_state": "aviso",
            "new_state": "delivery_hand_over",
            "state_text": "Aufgegeben",
            "direction": "incoming",
        },
        {
            "tracking_number": "1003",
            "title": "Gift",
            "sender": "Shop GmbH",
            "old_state": "in_distribution",
            "new_state": "unknown",
            "state_text": "somethingNew",
            "direction": "outgoing",
        },
    ]


async def test_no_event_on_first_refresh(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Setting up the entry does not fire events."""
    events = async_capture_events(hass, EVENT_SHIPMENT_UPDATED)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert events == []
