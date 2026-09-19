import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from booking.domain import BookingStatus

# Identifiers of other systems: a safe alphabet only. Postgres rejects NUL bytes in
# text, so an unrestricted string was a 500 (found by fuzzing GET /slots).
EXTERNAL_ID_PATTERN = r"^[A-Za-z0-9._:@-]+$"
ExternalId = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=EXTERNAL_ID_PATTERN)
]
# 10 million roubles. Also keeps the value inside a 32-bit Postgres integer:
# without it a huge price was a 500 (found by fuzzing payments, same bug here).
MAX_AMOUNT_MINOR = 1_000_000_000


class SlotCreate(BaseModel):
    master_id: ExternalId
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
    slot_id: uuid.UUID
    client_id: ExternalId


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slot_id: uuid.UUID
    client_id: str
    status: BookingStatus
    created_at: datetime
    updated_at: datetime


class PaymentOut(BaseModel):
    payment_id: uuid.UUID
    status: str
    checkout_url: str | None


class ErrorOut(BaseModel):
    error: str
    detail: str
