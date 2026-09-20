"""Use cases of the payments service: the only place that talks to the database."""

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from payments.domain import (
    CONFIRM_BOOKING,
    PaymentConflictError,
    PaymentNotFoundError,
    PaymentStatus,
    can_transition,
    target_status,
)
from payments.models import ONE_PAYMENT_PER_BOOKING, OutboxMessage, Payment, ProviderEvent
from payments.paystub import PayStubClient

WebhookOutcome = Literal["processed", "duplicate", "ignored"]


def create_payment(
    session: Session,
    paystub: PayStubClient,
    booking_id: uuid.UUID,
    amount_minor: int,
    currency: str,
    now: datetime,
) -> tuple[Payment, bool]:
    """Create the payment of a booking, or return the one that exists (ADR-0008).

    Returns the payment and whether it was created by this call.
    """
    payment = Payment(
        booking_id=booking_id,
        amount_minor=amount_minor,
        currency=currency,
        status=PaymentStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    session.add(payment)
    created = True
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        if _violated_constraint(error) != ONE_PAYMENT_PER_BOOKING:
            raise
        created = False
        payment = session.scalars(select(Payment).where(Payment.booking_id == booking_id)).one()
        if (payment.amount_minor, payment.currency) != (amount_minor, currency):
            raise PaymentConflictError(
                f"booking {booking_id} already has a payment of "
                f"{payment.amount_minor} {payment.currency}"
            ) from error

    if payment.provider_charge_id is None:
        # First attempt, or a retry after the provider failed: ask for the charge.
        payment_id = payment.id
        # Do not hold a database transaction open during a network call.
        session.rollback()
        charge = paystub.create_charge(payment_id, amount_minor, currency)
        session.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.provider_charge_id.is_(None))
            .values(provider_charge_id=charge.id, checkout_url=charge.checkout_url, updated_at=now)
        )
        session.commit()
        payment = session.get_one(Payment, payment_id)
    return payment, created


def get_payment(session: Session, payment_id: uuid.UUID) -> Payment:
    payment = session.get(Payment, payment_id)
    if payment is None:
        raise PaymentNotFoundError(f"payment {payment_id} does not exist")
    return payment


def handle_webhook(
    session: Session,
    event_id: str,
    event_type: str,
    reference: str,
    now: datetime,
) -> WebhookOutcome:
    """Apply a provider event exactly once (ADR-0008).

    Answering non-2xx makes the provider retry, so an error is returned only
    when a retry can help. Anything a retry cannot fix is acknowledged.

    Confirming the booking is not done here: the command goes into the outbox
    and the relay delivers it, so no transaction waits for a neighbour's
    answer (ADR-0010).
    """
    target = target_status(event_type)
    if target is None:
        return "ignored"

    # The primary key decides: a concurrent duplicate waits here for the first
    # one to finish, then finds the event already recorded.
    recorded = session.scalar(
        insert(ProviderEvent)
        .values(event_id=event_id, event_type=event_type, received_at=now)
        .on_conflict_do_nothing(index_elements=[ProviderEvent.event_id])
        .returning(ProviderEvent.event_id)
    )
    if recorded is None:
        session.rollback()
        return "duplicate"

    payment = _find_payment(session, reference)
    if payment is None or not can_transition(PaymentStatus(payment.status), target):
        # Unknown payment, the same status again, or an event out of order.
        session.commit()
        return "ignored"

    payment.status = target
    payment.updated_at = now
    if target == PaymentStatus.SUCCEEDED:
        session.add(
            OutboxMessage(
                topic=CONFIRM_BOOKING,
                payload={
                    "payment_id": str(payment.id),
                    "booking_id": str(payment.booking_id),
                },
            )
        )
    # One transaction: the payment is succeeded and the command exists, or neither.
    session.commit()
    return "processed"


def _find_payment(session: Session, reference: str) -> Payment | None:
    try:
        payment_id = uuid.UUID(reference)
    except ValueError:
        return None
    # Lock the row: two different events for one payment must not interleave.
    return session.get(Payment, payment_id, with_for_update=True)


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)
