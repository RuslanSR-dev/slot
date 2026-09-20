import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from booking.domain import ACTIVE_STATUSES, BookingStatus

# The database guarantees at most one active booking per slot (ADR-0005).
ACTIVE_SLOT_INDEX = "uq_bookings_active_slot"


class Base(DeclarativeBase):
    pass


class Slot(Base):
    __tablename__ = "slots"
    __table_args__ = (Index("ix_slots_master_starts_at", "master_id", "starts_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    master_id: Mapped[str] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Minor units (kopecks): money is never a float.
    price_minor: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _sql_list(statuses: Iterable[BookingStatus]) -> str:
    return ", ".join(f"'{status}'" for status in sorted(statuses))


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint(f"status IN ({_sql_list(BookingStatus)})", name="ck_bookings_status"),
        Index("ix_bookings_slot_id", "slot_id"),
        # "My bookings" asks for every booking of one client; without this it
        # is a sequential scan over the whole table.
        Index("ix_bookings_client_id", "client_id"),
        Index(
            ACTIVE_SLOT_INDEX,
            "slot_id",
            unique=True,
            postgresql_where=text(f"status IN ({_sql_list(ACTIVE_STATUSES)})"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    slot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("slots.id"))
    client_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxMessage(Base):
    """A message that must reach the broker.

    Written in the same transaction as the change it describes, so there is no
    moment when the booking is confirmed but the event does not exist (ADR-0010).
    The id is time-ordered (uuid7), so ordering by it is creation order.
    """

    __tablename__ = "outbox_messages"
    __table_args__ = (
        # Only unpublished rows are ever queried; published ones stay for the audit.
        Index(
            "ix_outbox_messages_unpublished",
            "id",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    topic: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Failed publish attempts: a growing number means the broker is unreachable.
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(200))
