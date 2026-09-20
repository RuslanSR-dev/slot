"""Delivers the commands the payments service wrote into its outbox.

Two commands live here (ADR-0010):

- `booking.confirm` - the money arrived, the booking may be confirmed. If
  booking refuses (the slot is long gone), the payment is marked for a
  refund and a refund command is written in the same transaction.
- `payment.refund` - give the money back through PayStub, with the payment
  id as the idempotency key so a retry cannot refund twice.

Run in the stack as `python -m payments.relay`.
"""

import logging
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from payments.app import Settings
from payments.booking_client import BookingClient
from payments.domain import CONFIRM_BOOKING, REFUND_PAYMENT, BookingRejectedError
from payments.models import OutboxMessage, Payment
from payments.paystub import PayStubClient

BATCH_SIZE = 100
POLL_SECONDS = 0.5
MAX_ERROR_LENGTH = 200

logger = logging.getLogger(__name__)


def process_pending(
    session: Session,
    booking: BookingClient,
    paystub: PayStubClient,
    now: datetime,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Deliver the unpublished commands in order. Returns how many were done.

    The row is locked while its command is being delivered, which is what
    keeps two relays from doing the same thing twice.
    """
    messages = session.scalars(
        select(OutboxMessage)
        .where(OutboxMessage.published_at.is_(None))
        .order_by(OutboxMessage.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()

    done = 0
    for message in messages:
        try:
            _deliver(session, booking, paystub, message, now)
        except Exception as error:
            # The neighbour is unavailable. Keep the command, count the attempt
            # and stop: the commands behind it would fail the same way.
            session.rollback()
            _record_failure(session, message.id, str(error))
            logger.warning("outbox command %s failed: %s", message.topic, error)
            break
        message.published_at = now
        done += 1
    session.commit()
    return done


def _deliver(
    session: Session,
    booking: BookingClient,
    paystub: PayStubClient,
    message: OutboxMessage,
    now: datetime,
) -> None:
    if message.topic == CONFIRM_BOOKING:
        _confirm_booking(session, booking, message.payload, now)
    elif message.topic == REFUND_PAYMENT:
        _refund_payment(session, paystub, message.payload, now)
    else:
        raise ValueError(f"unknown outbox command {message.topic}")


def _confirm_booking(
    session: Session, booking: BookingClient, payload: dict[str, Any], now: datetime
) -> None:
    payment_id = uuid.UUID(payload["payment_id"])
    try:
        booking.confirm(uuid.UUID(payload["booking_id"]))
    except BookingRejectedError:
        # The booking expired or was cancelled before the money arrived. The
        # client is not getting the slot, so they are getting the money back.
        session.execute(
            update(Payment)
            .where(Payment.id == payment_id)
            .values(needs_refund=True, updated_at=now)
        )
        session.add(OutboxMessage(topic=REFUND_PAYMENT, payload={"payment_id": str(payment_id)}))


def _refund_payment(
    session: Session, paystub: PayStubClient, payload: dict[str, Any], now: datetime
) -> None:
    payment = session.get_one(Payment, uuid.UUID(payload["payment_id"]))
    if payment.refunded_at is not None:
        # Already refunded: the command is done, nothing to ask PayStub for.
        return
    if payment.provider_charge_id is None:
        raise ValueError(f"payment {payment.id} has no charge to refund")
    refund = paystub.create_refund(payment.id, payment.provider_charge_id, payment.amount_minor)
    session.execute(
        update(Payment)
        .where(Payment.id == payment.id)
        .values(
            needs_refund=False,
            provider_refund_id=refund.id,
            refunded_at=now,
            updated_at=now,
        )
    )


def touch_heartbeat(path: str | None, now: datetime) -> None:
    """Say that the loop is still turning.

    A process without an HTTP port has nothing to answer a healthcheck with.
    Writing the time of every cycle into a file gives the container something
    to check that is not "the process exists": a loop stuck on a call nobody
    times out stops updating it.
    """
    if path is None:
        return
    Path(path).write_text(now.isoformat())


def _record_failure(session: Session, message_id: uuid.UUID, error: str) -> None:
    session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.id == message_id)
        .values(attempts=OutboxMessage.attempts + 1, last_error=error[:MAX_ERROR_LENGTH])
    )
    session.commit()


def run_forever(
    session_factory: sessionmaker[Session],
    booking: BookingClient,
    paystub: PayStubClient,
    poll: float = POLL_SECONDS,
    heartbeat: str | None = None,
) -> None:  # pragma: no cover - the loop is the process, its body is tested
    while True:
        now = datetime.now(UTC)
        touch_heartbeat(heartbeat, now)
        with session_factory() as session:
            done = process_pending(session, booking, paystub, now)
        if done == 0:
            time.sleep(poll)


def main() -> None:  # pragma: no cover - entry point of the relay container
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    engine = create_engine(settings.database_url, pool_size=2, pool_pre_ping=True)
    booking = BookingClient(settings.booking_url, settings.http_timeout)
    paystub = PayStubClient(settings.paystub_url, settings.paystub_api_key, settings.http_timeout)
    run_forever(
        sessionmaker(engine), booking, paystub, heartbeat=os.environ.get("SLOT_HEARTBEAT_FILE")
    )


if __name__ == "__main__":
    main()
