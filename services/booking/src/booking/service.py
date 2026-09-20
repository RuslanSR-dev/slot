"""Use cases of the booking service: the only place that talks to the database."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import ColumnElement, and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from booking import events
from booking.auth import Identity, Role, ensure_owner
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
from booking.models import ACTIVE_SLOT_INDEX, Booking, OutboxMessage, Slot


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
    expired = session.execute(
        update(Booking)
        .where(
            Booking.slot_id == slot_id,
            Booking.status == BookingStatus.PENDING,
            Booking.created_at <= now - pending_ttl,
        )
        .values(status=BookingStatus.EXPIRED, updated_at=now)
        .returning(Booking.id, Booking.client_id)
    ).all()
    for expired_id, expired_client in expired:
        _enqueue_event(session, BookingStatus.EXPIRED, expired_id, expired_client, slot, now)

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
    """Without an owner check: only for the internal endpoint payments calls."""
    booking = session.get(Booking, booking_id)
    if booking is None:
        raise BookingNotFoundError(f"booking {booking_id} does not exist")
    return booking


def get_owned_booking(
    session: Session, booking_id: uuid.UUID, identity: Identity
) -> tuple[Booking, Slot]:
    """The booking together with its slot, and only if the caller is a side of it.

    Every path a person can reach goes through here: the rule lives in one
    place, not in a check repeated in each endpoint (and forgotten in one).
    """
    booking = get_booking(session, booking_id)
    slot = session.get_one(Slot, booking.slot_id)
    ensure_owner(identity, booking.client_id, slot.master_id)
    return booking, slot


def list_my_bookings(session: Session, identity: Identity) -> list[tuple[Booking, Slot]]:
    """What the caller is a side of: their own bookings, or those on their slots.

    The same rule as for a single booking, written as a filter: the caller
    cannot ask for someone else's list, because there is nothing to ask with.
    """
    mine = (
        Booking.client_id == identity.subject
        if identity.role is Role.CLIENT
        else Slot.master_id == identity.subject
    )
    query = (
        select(Booking, Slot)
        .join(Slot, Slot.id == Booking.slot_id)
        .where(mine)
        .order_by(Slot.starts_at)
    )
    return [(booking, slot) for booking, slot in session.execute(query)]


def _is_stale(booking: Booking, now: datetime, pending_ttl: timedelta) -> bool:
    """Pending past its time: the slot is free again, only nobody has said so yet."""
    return booking.status == BookingStatus.PENDING and booking.created_at <= now - pending_ttl


def get_payable_booking(
    session: Session,
    booking_id: uuid.UUID,
    identity: Identity,
    now: datetime,
    pending_ttl: timedelta,
) -> tuple[Booking, Slot]:
    """A booking can be paid while it is pending and still holds its slot."""
    booking, slot = get_owned_booking(session, booking_id, identity)
    if booking.status != BookingStatus.PENDING or _is_stale(booking, now, pending_ttl):
        raise BookingNotPayableError(f"booking {booking_id} is {booking.status} and cannot be paid")
    return booking, slot


def cancel_booking(
    session: Session, booking_id: uuid.UUID, identity: Identity, now: datetime
) -> Booking:
    """Cancelling is the one write a stranger could do the most damage with."""
    booking, _ = get_owned_booking(session, booking_id, identity)
    return change_status(session, booking.id, BookingStatus.CANCELLED, now=now)


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
    slot = session.get_one(Slot, booking.slot_id)
    _enqueue_event(session, target, booking_id, booking.client_id, slot, now)
    session.commit()
    session.refresh(booking)
    return booking


def _enqueue_event(
    session: Session,
    status: BookingStatus,
    booking_id: uuid.UUID,
    client_id: str,
    slot: Slot,
    now: datetime,
) -> None:
    """Write the event into the outbox, in the caller's transaction (ADR-0010).

    Nothing is published here: the relay does that. The transaction either
    commits the new state together with the event, or neither of them.
    """
    topic = events.event_type(status)
    if topic is None:
        return
    # The message id is also the event id the consumer deduplicates by.
    message_id = uuid.uuid7()
    session.add(
        OutboxMessage(
            id=message_id,
            topic=topic,
            payload=events.build_event(
                event_id=message_id,
                status=status,
                booking_id=booking_id,
                slot_id=slot.id,
                client_id=client_id,
                starts_at=slot.starts_at,
                occurred_at=now,
            ),
        )
    )
