import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from payments.domain import PaymentStatus

# One payment per booking (ADR-0008).
ONE_PAYMENT_PER_BOOKING = "uq_payments_booking_id"


class Base(DeclarativeBase):
    pass


_STATUS_VALUES = ", ".join(f"'{status}'" for status in sorted(PaymentStatus))


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("booking_id", name=ONE_PAYMENT_PER_BOOKING),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="ck_payments_status"),
        CheckConstraint("amount_minor > 0", name="ck_payments_amount_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    booking_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16))
    provider_charge_id: Mapped[str | None] = mapped_column(String(64))
    checkout_url: Mapped[str | None] = mapped_column(String(512))
    # Paid, but the booking could not be confirmed any more: money must go back.
    needs_refund: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProviderEvent(Base):
    """Every provider event we acted on. The primary key makes a repeat a no-op."""

    __tablename__ = "provider_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
