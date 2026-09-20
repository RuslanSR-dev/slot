"""Unit level: reading an event and writing the text, without anything around it."""

from datetime import UTC, datetime

import pytest

from notifier.domain import (
    MESSAGES,
    REQUIRED_FIELDS,
    BookingEvent,
    InvalidEventError,
    parse_event,
    render,
)

STARTS_AT = datetime(2026, 9, 21, 10, 30, tzinfo=UTC)
EVENT: dict[str, object] = {
    "event_id": "01998a2b-0000-7000-8000-00000000000e",
    "type": "booking.confirmed",
    "version": 1,
    "occurred_at": "2026-09-20T12:00:00+00:00",
    "booking_id": "01998a2b-0000-7000-8000-00000000000b",
    "slot_id": "01998a2b-0000-7000-8000-00000000000a",
    "client_id": "client-1",
    "starts_at": "2026-09-21T10:30:00+00:00",
}


def event(**overrides: object) -> BookingEvent:
    return parse_event(EVENT | overrides)


class TestParsing:
    def test_reads_the_fields_the_notifier_needs(self) -> None:
        assert event() == BookingEvent(
            event_id=str(EVENT["event_id"]),
            type="booking.confirmed",
            booking_id=str(EVENT["booking_id"]),
            client_id="client-1",
            starts_at=STARTS_AT,
        )

    def test_a_field_we_do_not_know_is_ignored(self) -> None:
        """A producer may add fields at any time; that must not break us."""
        assert event(loyalty_points=10).client_id == "client-1"

    @pytest.mark.parametrize("field", REQUIRED_FIELDS)
    def test_a_missing_field_is_not_an_event(self, field: str) -> None:
        with pytest.raises(InvalidEventError) as error:
            parse_event({key: value for key, value in EVENT.items() if key != field})

        assert field in str(error.value)

    def test_a_broken_date_is_not_an_event(self) -> None:
        with pytest.raises(InvalidEventError):
            event(starts_at="tomorrow-ish")


class TestText:
    @pytest.mark.parametrize("event_type", sorted(MESSAGES))
    def test_every_known_event_has_a_text_with_the_time_in_it(self, event_type: str) -> None:
        text = render(event(type=event_type))

        assert text is not None
        assert "2026-09-21 10:30" in text

    def test_confirmation_says_it_is_confirmed(self) -> None:
        assert render(event()) == "Your booking on 2026-09-21 10:30 is confirmed."

    def test_cancellation_says_it_is_cancelled(self) -> None:
        assert render(event(type="booking.cancelled")) == (
            "Your booking on 2026-09-21 10:30 is cancelled."
        )

    def test_expiry_says_the_slot_is_free(self) -> None:
        assert render(event(type="booking.expired")) == (
            "Your booking on 2026-09-21 10:30 expired, the slot is free again."
        )

    def test_an_event_we_do_not_know_produces_no_message(self) -> None:
        """A new event type on the producer side must not stop the consumer."""
        assert render(event(type="booking.rescheduled")) is None
