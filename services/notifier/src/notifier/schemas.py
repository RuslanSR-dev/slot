from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from notifier.domain import NotificationStatus

# Identifiers of other systems: a safe alphabet only, like in booking.
ExternalId = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:@-]+$")
]


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    booking_id: str
    client_id: str
    event_type: str
    text: str
    status: NotificationStatus
    attempts: int
    created_at: datetime


class ErrorOut(BaseModel):
    error: str
    detail: str
