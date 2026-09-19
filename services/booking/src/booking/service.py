"""Use cases of the booking service: the only place that talks to the database."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import ColumnElement, and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from booking.domain import (
    BookingNotFoundError,
    BookingNotPayableError,
    BookingStatus,
    InvalidTransitionError,
    SlotAlreadyBookedError,
    SlotNotFoundError,
    ensure_bookable,
    ensure_transition,
    ensure_valid_slot,
)
from booking.models import ACTIVE_SLOT_INDEX, Booking, Slot


def create_slot(
    session: Session,
    master_id: str,
    starts_at: datetime,
    ends_at: datetime,
    price_minor: int,
    now: datetime,
) -> Slot:
    ensure_valid_slot(starts_at, ends_at, now)
    slot = Slot(master_id=master_id, starts_at=starts_at, ends_at=ends_at, price_minor=price_minor)
    session.add(slot)
    session.commit()
    return slot


def _holds_slot(now: datetime, pending_ttl: timedelta) -> ColumnElement[bool]:
    """A booking holds its slot if it is confirmed, or pending and not stale (ADR-0008)."""
    return or_(
        Booking.status == BookingStatus.CONFIRMED,
        and_(Booking.status == BookingStatus.PENDING, Booking.created_at > now - pending_ttl),
    )


def list_slots(
    session: Session, master_id: str, day: date | None, now: datetime, pending_ttl: timedelta
) -> list[tuple[Slot, bool]]:
    """Slots of a master with a flag "can be booked right now"."""
    taken = exists().where(Booking.slot_id == Slot.id, _holds_slot(now, pending_ttl))
    query = select(Slot, taken).where(Slot.master_id == master_id).order_by(Slot.starts_at)
    if day is not None:
        day_start = datetime.combine(day, time.min, tzinfo=UTC)
        query = query.where(Slot.starts_at >= day_start, Slot.starts_at < day_start + timedelta(1))
    return [
        (slot, not is_taken and slot.starts_at > now) for slot, is_taken in session.execute(query)
    ]


def book_slot(
    session: Session, slot_id: uuid.UUID, client_id: str, now: datetime, pending_ttl: timedelta
) -> Booking:
    slot = session.get(Slot, slot_id)
    if slot is None:
        raise SlotNotFoundError(f"slot {slot_id} does not exist")
    ensure_bookable(slot.starts_at, now)

    # Stale unpaid bookings stop holding the slot at the moment someone wants it:
    # no scheduler is needed (ADR-0008).
    session.execute(
        update(Booking)
        .where(
            Booking.slot_id == slot_id,
            Booking.status == BookingStatus.PENDING,
            Booking.created_at <= now - pending_ttl,
        )
        .values(status=BookingStatus.EXPIRED, updated_at=now)
    )

    # No "is it free?" check here: two concurrent requests would both pass it.
    # The database index decides who wins (ADR-0005).
    booking = Booking(
        slot_id=slot_id,
        client_id=client_id,
        status=BookingStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    session.add(booking)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        if _violated_constraint(error) == ACTIVE_SLOT_INDEX:
            raise SlotAlreadyBookedError(f"slot {slot_id} is already booked") from error
        raise
    return booking


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


def get_booking(session: Session, booking_id: uuid.UUID) -> Booking:
    booking = session.get(Booking, booking_id)
    if booking is None:
        raise BookingNotFoundError(f"booking {booking_id} does not exist")
    return booking


def get_payable_booking(
    session: Session, booking_id: uuid.UUID, now: datetime, pending_ttl: timedelta
) -> tuple[Booking, Slot]:
    """A booking can be paid while it is pending and still holds its slot."""
    booking = get_booking(session, booking_id)
    if booking.status != BookingStatus.PENDING or booking.created_at <= now - pending_ttl:
        raise BookingNotPayableError(f"booking {booking_id} is {booking.status} and cannot be paid")
    slot = session.get_one(Slot, booking.slot_id)
    return booking, slot


def change_status(
    session: Session, booking_id: uuid.UUID, target: BookingStatus, now: datetime
) -> Booking:
    booking = get_booking(session, booking_id)
    current = BookingStatus(booking.status)
    # Repeating a transition that already happened is a success, not an error:
    # retries after a lost response must be safe (ADR-0008).
    if current == target:
        return booking
    ensure_transition(current, target)

    # Conditional update: if a concurrent request changed the status after we
    # read it, no row matches and the transition is rejected (ADR-0005).
    changed = session.scalar(
        update(Booking)
        .where(Booking.id == booking_id, Booking.status == current)
        .values(status=target, updated_at=now)
        .returning(Booking.id)
    )
    if changed is None:
        session.rollback()
        raise InvalidTransitionError(f"booking {booking_id} was changed by another request")
    session.commit()
    session.refresh(booking)
    return booking
