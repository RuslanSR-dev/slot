"""Unit level: the event body is pure data, so it is checked without a database.

The whole dictionary is compared at once on purpose. Field names are the
contract with the consumer, and a test that checks only a couple of fields
would not notice a renamed one.
"""

import uuid
from datetime import UTC, datetime

import pytest

from booking import events
from booking.domain import BookingStatus
from booking.schemas import BookingEventV1

EVENT_ID = uuid.UUID("01998a2b-0000-7000-8000-00000000000e")
BOOKING_ID = uuid.UUID("01998a2b-0000-7000-8000-00000000000b")
SLOT_ID = uuid.UUID("01998a2b-0000-7000-8000-00000000000a")
OCCURRED_AT = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
STARTS_AT = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (BookingStatus.CONFIRMED, "booking.confirmed"),
        (BookingStatus.CANCELLED, "booking.cancelled"),
        (BookingStatus.EXPIRED, "booking.expired"),
    ],
)
def test_status_change_produces_its_event(status: BookingStatus, expected: str) -> None:
    assert events.event_type(status) == expected


def test_holding_a_slot_is_nobody_elses_business() -> None:
    assert events.event_type(BookingStatus.PENDING) is None


def test_event_carries_everything_the_consumer_needs() -> None:
    """No consumer should have to call us back to act on an event (ADR-0010)."""
    event = events.build_event(
        event_id=EVENT_ID,
        status=BookingStatus.CONFIRMED,
        booking_id=BOOKING_ID,
        slot_id=SLOT_ID,
        client_id="client-1",
        starts_at=STARTS_AT,
        occurred_at=OCCURRED_AT,
    )

    assert event == {
        "event_id": str(EVENT_ID),
        "type": "booking.confirmed",
        "version": 1,
        "occurred_at": "2026-09-20T12:00:00+00:00",
        "booking_id": str(BOOKING_ID),
        "slot_id": str(SLOT_ID),
        "client_id": "client-1",
        "starts_at": "2026-09-21T10:00:00+00:00",
    }


def test_built_event_matches_the_published_schema() -> None:
    """The builder and the committed schema describe the same message."""
    event = events.build_event(
        event_id=EVENT_ID,
        status=BookingStatus.EXPIRED,
        booking_id=BOOKING_ID,
        slot_id=SLOT_ID,
        client_id="client-1",
        starts_at=STARTS_AT,
        occurred_at=OCCURRED_AT,
    )

    parsed = BookingEventV1.model_validate(event)

    assert parsed.type == events.BookingEventType.EXPIRED
    assert parsed.booking_id == BOOKING_ID
