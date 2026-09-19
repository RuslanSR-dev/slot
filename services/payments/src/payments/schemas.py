import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from payments.domain import PaymentStatus


class PaymentCreate(BaseModel):
    booking_id: uuid.UUID
    amount_minor: Annotated[int, Field(gt=0, description="Amount in kopecks")]
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

    id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    type: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    data: WebhookData


class WebhookAck(BaseModel):
    outcome: Literal["processed", "duplicate", "ignored"]


class ErrorOut(BaseModel):
    error: str
    detail: str
