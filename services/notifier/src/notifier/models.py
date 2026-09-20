from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from notifier.domain import NotificationStatus


class Base(DeclarativeBase):
    pass


_STATUS_VALUES = ", ".join(f"'{status}'" for status in sorted(NotificationStatus))


class Notification(Base):
    """One row per event, keyed by the event id (ADR-0010).

    The primary key is what makes the consumer idempotent: a second delivery
    of the same event cannot insert a second row, so it cannot send a second
    message. The row is also the answer to "was the client told?".
    """

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="ck_notifications_status"),
        Index("ix_notifications_booking_id", "booking_id"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    booking_id: Mapped[str] = mapped_column(String(64))
    client_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16))
    # Deliveries of this event that did not end with a sent message.
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(200))
    gateway_message_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
