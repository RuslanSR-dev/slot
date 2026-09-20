import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from booking.domain import BookingStatus
from booking.events import BookingEventType

# Identifiers of other systems: a safe alphabet only. Postgres rejects NUL bytes in
# text, so an unrestricted string was a 500 (found by fuzzing GET /slots).
EXTERNAL_ID_PATTERN = r"^[A-Za-z0-9._:@-]+$"
MAX_EXTERNAL_ID_LENGTH = 64
ExternalId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_EXTERNAL_ID_LENGTH, pattern=EXTERNAL_ID_PATTERN),
]
# 10 million roubles. Also keeps the value inside a 32-bit Postgres integer:
# without it a huge price was a 500 (found by fuzzing payments, same bug here).
MAX_AMOUNT_MINOR = 1_000_000_000


class SlotCreate(BaseModel):
    """Who the master is comes from the token, not from the body: a caller
    must not be able to publish slots in someone else's name."""

    starts_at: AwareDatetime
    ends_at: AwareDatetime
    price_minor: Annotated[int, Field(gt=0, le=MAX_AMOUNT_MINOR, description="Price in kopecks")]


class SlotOut(BaseModel):
    id: uuid.UUID
    master_id: str
    starts_at: datetime
    ends_at: datetime
    price_minor: int
    available: bool


class BookingCreate(BaseModel):
    """The client is taken from the token, for the same reason as the master."""

    slot_id: uuid.UUID


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slot_id: uuid.UUID
    client_id: str
    status: BookingStatus
    created_at: datetime
    updated_at: datetime


class MyBookingOut(BaseModel):
    """A booking with the slot it is for: what a person needs to see on one page.

    Without the slot fields every list would cost one more request per row,
    and the interface would have to read a slot that is not its business.
    """

    id: uuid.UUID
    slot_id: uuid.UUID
    client_id: str
    master_id: str
    status: BookingStatus
    starts_at: datetime
    ends_at: datetime
    price_minor: int
    created_at: datetime
    updated_at: datetime


class PaymentOut(BaseModel):
    payment_id: uuid.UUID
    status: str
    checkout_url: str | None


class ErrorOut(BaseModel):
    error: str
    detail: str


class BookingEventV1(BaseModel):
    """The published shape of a booking event (contracts/events/booking.v1.json).

    The consumer reads these fields and nothing else. Changing them is a
    breaking change, which is why the schema is committed and compared with
    `main` in CI, exactly like the OpenAPI schemas (ADR-0007).
    """

    event_id: uuid.UUID
    type: BookingEventType
    # Literally 1: a type checker needs a constant here. That it is the same 1 as
    # events.EVENT_VERSION is proven by validating a built event in the unit tests.
    version: Literal[1]
    occurred_at: AwareDatetime
    booking_id: uuid.UUID
    slot_id: uuid.UUID
    client_id: ExternalId
    starts_at: AwareDatetime
