"""What the notifier does with an event: no database, no broker, no HTTP.

`REQUIRED_FIELDS` is the contract with the producer. The notifier reads
these fields and nothing else, which is what its contract test writes into
contracts/events/consumers and compares with the published schema.

Error messages are marked `pragma: no mutate`: the contract is the error
`code`, the message is a hint for humans.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

# Everything the notifier needs to act on an event alone (ADR-0010, п. 6).
REQUIRED_FIELDS = ("event_id", "type", "booking_id", "client_id", "starts_at")

# How the time of the booking is shown to the client.
WHEN_FORMAT = "%Y-%m-%d %H:%M"

# Texts we send. An event type that is not here is acknowledged and skipped:
# a consumer must survive a producer that learned a new event (ADR-0010).
MESSAGES: dict[str, str] = {
    "booking.confirmed": "Your booking on {when} is confirmed.",
    "booking.cancelled": "Your booking on {when} is cancelled.",
    "booking.expired": "Your booking on {when} expired, the slot is free again.",
}


class NotificationStatus(StrEnum):
    # Claimed by a consumer, not yet delivered. A second delivery of the same
    # event finds this and retries with the same idempotency key.
    SENDING = "sending"
    SENT = "sent"
    # Gave up after too many deliveries; the message is in the dead-letter stream.
    DEAD = "dead"


class DomainError(Exception):
    """A request or a message that breaks a rule. `code` goes to the API response."""

    code: str = "domain_error"


class InvalidEventError(DomainError):
    """The message is not an event we can act on. Retrying it cannot help."""

    code = "invalid_event"


class GatewayUnavailableError(DomainError):
    """The gateway did not accept the message. Retrying it can help."""

    code = "gateway_unavailable"


@dataclass(frozen=True)
class BookingEvent:
    event_id: str
    type: str
    booking_id: str
    client_id: str
    starts_at: datetime


def parse_event(data: dict[str, object]) -> BookingEvent:
    """Read the fields we need. Anything else in the message is ignored."""
    missing = [field for field in REQUIRED_FIELDS if not data.get(field)]
    if missing:
        raise InvalidEventError(f"event is missing {', '.join(missing)}")  # pragma: no mutate
    try:
        starts_at = datetime.fromisoformat(str(data["starts_at"]))
    except ValueError as error:
        raise InvalidEventError(f"starts_at is not a date: {error}") from error  # pragma: no mutate
    return BookingEvent(
        event_id=str(data["event_id"]),
        type=str(data["type"]),
        booking_id=str(data["booking_id"]),
        client_id=str(data["client_id"]),
        starts_at=starts_at,
    )


def render(event: BookingEvent) -> str | None:
    """The text for the client, or None when this event is not worth a message."""
    template = MESSAGES.get(event.type)
    if template is None:
        return None
    return template.format(when=event.starts_at.strftime(WHEN_FORMAT))
