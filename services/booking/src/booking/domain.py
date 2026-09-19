"""Booking rules that do not depend on the database or HTTP.

Time is always passed in as `now`: code that reads the clock itself cannot
be tested for "the slot has just started" without waiting for real time.
"""

from datetime import datetime
from enum import StrEnum


class BookingStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# A slot can have at most one booking in these statuses (ADR-0005).
ACTIVE_STATUSES: frozenset[BookingStatus] = frozenset(
    {BookingStatus.PENDING, BookingStatus.CONFIRMED}
)

ALLOWED_TRANSITIONS: dict[BookingStatus, frozenset[BookingStatus]] = {
    BookingStatus.PENDING: frozenset(
        {BookingStatus.CONFIRMED, BookingStatus.CANCELLED, BookingStatus.EXPIRED}
    ),
    BookingStatus.CONFIRMED: frozenset({BookingStatus.CANCELLED}),
    BookingStatus.CANCELLED: frozenset(),
    BookingStatus.EXPIRED: frozenset(),
}


class DomainError(Exception):
    """A request that breaks a business rule. `code` goes to the API response."""

    code: str = "domain_error"


class InvalidSlotError(DomainError):
    code = "invalid_slot"


class SlotInPastError(DomainError):
    code = "slot_in_past"


class SlotAlreadyBookedError(DomainError):
    code = "slot_already_booked"


class InvalidTransitionError(DomainError):
    code = "invalid_transition"


class SlotNotFoundError(DomainError):
    code = "slot_not_found"


class BookingNotFoundError(DomainError):
    code = "booking_not_found"


def ensure_valid_slot(starts_at: datetime, ends_at: datetime, now: datetime) -> None:
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        raise InvalidSlotError("slot times must include a timezone")
    if ends_at <= starts_at:
        raise InvalidSlotError("slot must end after it starts")
    if starts_at <= now:
        raise InvalidSlotError("slot must start in the future")


def ensure_bookable(starts_at: datetime, now: datetime) -> None:
    if starts_at <= now:
        raise SlotInPastError("slot has already started")


def ensure_transition(current: BookingStatus, target: BookingStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"cannot move booking from {current} to {target}")
