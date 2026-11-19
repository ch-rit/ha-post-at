"""Constants for the Österreichische Post integration."""

from __future__ import annotations

import logging
from typing import Final

from post_at.models import NotificationClaim

DOMAIN: Final = "post_at"
LOGGER = logging.getLogger(__package__)

CONF_SESSION: Final = "session"
CONF_REDIRECT_URL: Final = "redirect_url"
CONF_KEEP_DELIVERED_DAYS: Final = "keep_delivered_days"

DEFAULT_SCAN_INTERVAL: Final = 15
"""Minutes."""
MIN_SCAN_INTERVAL: Final = 5
DEFAULT_KEEP_DELIVERED_DAYS: Final = 3
MAX_KEEP_DELIVERED_DAYS: Final = 30

EVENT_SHIPMENT_UPDATED: Final = "post_at_shipment_updated"

DIRECTION_INCOMING: Final = "incoming"
DIRECTION_OUTGOING: Final = "outgoing"

STATE_DELIVERED: Final = "delivered"
STATE_UNKNOWN: Final = "unknown"
SHIPMENT_STATES: Final = [
    "aviso",
    "delivery_hand_over",
    "in_distribution",
    "in_delivery",
    STATE_DELIVERED,
    STATE_UNKNOWN,
]
"""Per-shipment sensor states: the library's camelCase ``state_key`` in snake_case."""

PICKUP_CLAIMS: Final = frozenset(
    {NotificationClaim.ABHOLBEREIT, NotificationClaim.ABHOLBEREIT_PERSOENLICH}
)

TARGET_PLACE: Final = "place"
TARGET_NEIGHBOR: Final = "neighbor"
TARGET_POST_OFFICE: Final = "post_office"
TARGET_PARCEL_LOCKER: Final = "parcel_locker"
TARGETS: Final = [TARGET_PLACE, TARGET_NEIGHBOR, TARGET_POST_OFFICE, TARGET_PARCEL_LOCKER]
