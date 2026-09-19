"""Use cases of the booking service: the only place that talks to the database."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from booking.domain import (
    ACTIVE_STATUSES,
    BookingNotFoundError,
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
    session: Session, master_id: str, starts_at: datetime, ends_at: datetime, now: datetime
) -> Slot:
    ensure_valid_slot(starts_at, ends_at, now)
    slot = Slot(master_id=master_id, starts_at=starts_at, ends_at=ends_at)
    session.add(slot)
    session.commit()
    return slot


def list_slots(
    session: Session, master_id: str, day: date | None, now: datetime
) -> list[tuple[Slot, bool]]:
    """Slots of a master with a flag "can be booked right now"."""
    taken = exists().where(Booking.slot_id == Slot.id, Booking.status.in_(ACTIVE_STATUSES))
    query = select(Slot, taken).where(Slot.master_id == master_id).order_by(Slot.starts_at)
    if day is not None:
        day_start = datetime.combine(day, time.min, tzinfo=UTC)
        query = query.where(Slot.starts_at >= day_start, Slot.starts_at < day_start + timedelta(1))
    return [
        (slot, not is_taken and slot.starts_at > now) for slot, is_taken in session.execute(query)
    ]


def book_slot(session: Session, slot_id: uuid.UUID, client_id: str, now: datetime) -> Booking:
    slot = session.get(Slot, slot_id)
    if slot is None:
        raise SlotNotFoundError(f"slot {slot_id} does not exist")
    ensure_bookable(slot.starts_at, now)

    # No "is it free?" check here: two concurrent requests would both pass it.
    # The database index decides who wins (ADR-0005).
    booking = Booking(slot_id=slot_id, client_id=client_id, status=BookingStatus.PENDING)
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


def change_status(
    session: Session, booking_id: uuid.UUID, target: BookingStatus, now: datetime
) -> Booking:
    booking = get_booking(session, booking_id)
    current = BookingStatus(booking.status)
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
