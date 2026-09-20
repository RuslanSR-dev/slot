"""Use cases of the notifier: the only place that talks to the database."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from notifier.models import Notification


def list_notifications(session: Session, booking_id: str) -> list[Notification]:
    """What the client was told about a booking, oldest first."""
    return list(
        session.scalars(
            select(Notification)
            .where(Notification.booking_id == booking_id)
            .order_by(Notification.created_at, Notification.event_id)
        )
    )
