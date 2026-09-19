import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func, text
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
