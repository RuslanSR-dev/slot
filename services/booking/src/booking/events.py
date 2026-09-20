"""Events the booking service tells the world about.

Pure functions: no database, no broker. The service writes what they return
into the outbox table, the relay publishes it (ADR-0010).

An event carries everything a consumer needs to act on it alone. Consumers
must not depend on the order of events or on reading our API back, because a
consumer group breaks the order on redelivery (ADR-0010, п. 6).
"""

import uuid
from datetime import datetime
from enum import StrEnum

from booking.domain import BookingStatus

# Version of the message shape. It is also part of the stream name: an
# incompatible change is a new stream, not a surprise for the consumers.
EVENT_VERSION = 1


class BookingEventType(StrEnum):
    """Published event names. Also the enum in the committed JSON schema."""

    CONFIRMED = "booking.confirmed"
    CANCELLED = "booking.cancelled"
    EXPIRED = "booking.expired"


# Statuses worth telling the world about. `pending` is not one of them:
# nobody outside cares that a slot is being held for 15 minutes.
STATUS_EVENTS: dict[BookingStatus, BookingEventType] = {
    BookingStatus.CONFIRMED: BookingEventType.CONFIRMED,
    BookingStatus.CANCELLED: BookingEventType.CANCELLED,
    BookingStatus.EXPIRED: BookingEventType.EXPIRED,
}


def event_type(status: BookingStatus) -> BookingEventType | None:
    """The event a status change produces, or None when it produces none."""
    return STATUS_EVENTS.get(status)


def build_event(
    event_id: uuid.UUID,
    status: BookingStatus,
    booking_id: uuid.UUID,
    slot_id: uuid.UUID,
    client_id: str,
    starts_at: datetime,
    occurred_at: datetime,
) -> dict[str, object]:
    """The message body. `event_id` is the natural key a consumer deduplicates by."""
    return {
        "event_id": str(event_id),
        "type": STATUS_EVENTS[status].value,
        "version": EVENT_VERSION,
        "occurred_at": occurred_at.isoformat(),
        "booking_id": str(booking_id),
        "slot_id": str(slot_id),
        "client_id": client_id,
        "starts_at": starts_at.isoformat(),
    }
