import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints

from booking.domain import BookingStatus

ExternalId = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class SlotCreate(BaseModel):
    master_id: ExternalId
    starts_at: AwareDatetime
    ends_at: AwareDatetime


class SlotOut(BaseModel):
    id: uuid.UUID
    master_id: str
    starts_at: datetime
    ends_at: datetime
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


class ErrorOut(BaseModel):
    error: str
    detail: str
