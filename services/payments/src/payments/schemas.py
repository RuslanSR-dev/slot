import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from payments.domain import PaymentStatus

# 10 million roubles. Also keeps the value inside a 32-bit Postgres integer:
# without it a huge amount was a 500 (found by fuzzing).
MAX_AMOUNT_MINOR = 1_000_000_000


class PaymentCreate(BaseModel):
    booking_id: uuid.UUID
    amount_minor: Annotated[int, Field(gt=0, le=MAX_AMOUNT_MINOR, description="Amount in kopecks")]
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    booking_id: uuid.UUID
    amount_minor: int
    currency: str
    status: PaymentStatus
    checkout_url: str | None
    needs_refund: bool


class WebhookData(BaseModel):
    charge_id: str
    reference: str


class WebhookEvent(BaseModel):
    """PayStub event, see contracts/paystub/openapi.yaml."""

    # Printable ASCII only: the values are stored, and Postgres rejects NUL bytes.
    id: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[ -~]+$")]
    type: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[ -~]+$")]
    data: WebhookData


class WebhookAck(BaseModel):
    outcome: Literal["processed", "duplicate", "ignored"]


class ErrorOut(BaseModel):
    error: str
    detail: str
