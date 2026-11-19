"""Fixtures for the Österreichische Post tests."""

from __future__ import annotations

import base64
from collections.abc import Generator
from datetime import timedelta
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from post_at import PostAtClient, Session
from post_at.models import Branch, Place, ShipmentList, ShipmentRedirections
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.post_at.const import CONF_SESSION, DOMAIN

ACCOUNT_ID = "sso-1234"
EMAIL = "user@example.com"
FIXTURES = Path(__file__).parent / "fixtures"

STATE_TEXTS = {
    "aviso": "Angekündigt",
    "deliveryHandOver": "Aufgegeben",
    "inDistribution": "In Verteilung",
    "inDelivery": "In Zustellung",
    "delivered": "Zugestellt",
}


def load_fixture(name: str) -> Any:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / name).read_text())


def make_session(
    refresh_token: str = "refresh-1", claims: dict[str, Any] | None = None
) -> Session:
    """Return a session with an (unsigned) ID token carrying ``claims``."""
    if claims is None:
        claims = {"extension_SSOID": ACCOUNT_ID, "username": EMAIL}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return Session(refresh_token, {}, f"e30.{payload}.signature")


def shipment_data(
    number: str,
    *states: str,
    title: str = "",
    sender: str | None = "Shop GmbH",
    incoming: bool = True,
    age: timedelta = timedelta(hours=1),
    **extra: Any,
) -> dict[str, Any]:
    """Return a shipment in wire format; the last state is ``age`` old."""
    now = dt_util.utcnow()
    states = states or ("aviso",)
    events = [
        {
            "timestamp": (now - age - timedelta(minutes=len(states) - 1 - i)).isoformat(),
            "trackingState": STATE_TEXTS.get(key, key),
            "trackingStateKey": key,
            "trackingDesc": f"Event {key}",
            "eventpostalcode": "1030",
            "eventPlaceName": "Wien",
        }
        for i, key in enumerate(states)
    ]
    return {
        "activeIdentityCode": number,
        "bezeichnung": title,
        "sender": sender,
        "isRecipient": incoming,
        "tags": [{"key": "empfangen" if incoming else "versenden"}],
        "weight": 1.5,
        "estimatedDeliveryDateText": "Mo, 23.11.",
        "deliveryType": "DELIVERY",
        "produktkategorie": "Paket",
        "sendungsEvents": events,
        "packageRedirections": [],
        **extra,
    }


def shipment_list(*shipments: dict[str, Any]) -> ShipmentList:
    """Return a shipment list of wire-format shipments."""
    incoming = sum(1 for shipment in shipments if shipment["isRecipient"])
    return ShipmentList.model_validate(
        {
            "totalSendungen": len(shipments),
            "versandSendungen": len(shipments) - incoming,
            "empfangsSendungen": incoming,
            "sendungen": list(shipments),
        }
    )


def default_shipments() -> ShipmentList:
    """Return the shipments of the default mocked account."""
    return shipment_list(
        shipment_data(
            "1001",
            "aviso",
            "deliveryHandOver",
            "inDelivery",
            title="Headphones",
            packageRedirections=[{"place": {"beschreibung": "Garage", "isSelected": True}}],
        ),
        shipment_data("1002", "aviso"),
        shipment_data("1003", "deliveryHandOver", "inDistribution", title="Gift", incoming=False),
        shipment_data(
            "1004",
            "inDistribution",
            sender=None,
            notificationInformation={"claim": "ABHOLBEREIT"},
            recipient={"name": "Max Mustermann", "street": "Hauptstraße 1", "postCode": "1030"},
        ),
        shipment_data("1005", "inDelivery", "delivered", title="Book", age=timedelta(days=1)),
        shipment_data("1006", "delivered", title="Old", age=timedelta(days=10)),
    )


def make_redirections(
    number: str,
    *,
    current: str = "DEFAULT",
    place: str = "ENABLED",
    neighbor: str = "DISABLED",
    post_office: str = "ENABLED",
    parcel_locker: str = "ENABLED",
    post_office_branches: list[str] | None = None,
    parcel_locker_branches: list[str] | None = None,
) -> ShipmentRedirections:
    """Return redirection options; ``current`` is the type currently set."""
    states = {
        "NEIGHBOR": neighbor,
        "PLACE": place,
        "POST_OFFICE": post_office,
        "PARCEL_LOCKER": parcel_locker,
        "DEFAULT": "ENABLED",
    }
    return ShipmentRedirections.model_validate(
        {
            "activeIdentityCode": number,
            "possibleRedirections": {
                "branchesPostOffice": post_office_branches,
                "branchesParcelLocker": parcel_locker_branches,
            },
            "redirectionInformation": {
                "redirectionOptions": [
                    {
                        "type": kind,
                        "state": state,
                        "current": kind == current,
                        "unavailabilityReasons": [] if state == "ENABLED" else ["SIGNATURE"],
                    }
                    for kind, state in states.items()
                ]
            },
        }
    )


def make_branch(key: str, name: str) -> Branch:
    """Return a branch."""
    return Branch.model_validate({"branchkey": key, "type": "FILIALE", "longname": name})


PLACES = [
    Place(key="Vor_Haustüre", text="Vor der Haustüre"),
    Place(key="sonstige", text="Sonstiger Ort"),
]


def entity_id(hass: HomeAssistant, key: str, domain: str = "sensor") -> str | None:
    """Return the entity id of an entity by its unique id suffix."""
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{ACCOUNT_ID}_{key}")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable the custom integration in all tests."""


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return the config entry of the default account."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=ACCOUNT_ID,
        data={CONF_SESSION: make_session().to_dict()},
    )


@pytest.fixture
def mock_client_class() -> Generator[MagicMock]:
    """Mock the library client used by the coordinator and the actions."""
    with (
        patch(
            "custom_components.post_at.coordinator.PostAtClient", autospec=True
        ) as client_class,
        patch("custom_components.post_at.services.PostAtClient", new=client_class),
    ):
        client = client_class.return_value
        client.__enter__.return_value = client
        client.shipments.return_value = default_shipments()
        client.possible_redirections.side_effect = lambda number: make_redirections(
            number, post_office_branches=["101"]
        )
        client.places.return_value = PLACES
        client.branches.return_value = [make_branch("101", "Postfiliale Landstraße")]
        yield client_class


@pytest.fixture
def mock_client(mock_client_class: MagicMock) -> PostAtClient:
    """Return the mocked client instance."""
    return mock_client_class.return_value


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Do not set up the entry created by a config flow."""
    with patch(
        "custom_components.post_at.async_setup_entry", return_value=True
    ) as setup_entry:
        yield setup_entry


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> MockConfigEntry:
    """Set up the integration with the default account."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
