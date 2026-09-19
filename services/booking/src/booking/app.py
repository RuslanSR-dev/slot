import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from booking import __version__, service
from booking.domain import (
    BookingNotFoundError,
    BookingStatus,
    DomainError,
    InvalidSlotError,
    SlotNotFoundError,
)
from booking.schemas import BookingCreate, BookingOut, ErrorOut, SlotCreate, SlotOut

SERVICE_NAME = "booking"
# Reported when the build did not say which commit it is. Deliberately loud:
# a plausible-looking default like "dev" would hide a broken pipeline.
UNKNOWN_BUILD = "unknown"

Clock = Callable[[], datetime]

_ERROR_STATUS: dict[type[DomainError], int] = {
    SlotNotFoundError: status.HTTP_404_NOT_FOUND,
    BookingNotFoundError: status.HTTP_404_NOT_FOUND,
    InvalidSlotError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


class Health(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    build_sha: str


@dataclass(frozen=True)
class Settings:
    database_url: str
    db_pool_size: int = 10

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get(
                "SLOT_DATABASE_URL", "postgresql+psycopg://slot:slot@localhost:5432/slot"
            ),
            db_pool_size=int(os.environ.get("SLOT_DB_POOL_SIZE", "10")),
        )


def system_clock() -> datetime:
    return datetime.now(UTC)


def create_app(settings: Settings | None = None, clock: Clock = system_clock) -> FastAPI:
    settings = settings or Settings.from_env()
    # Connections are opened lazily: creating the app does not need a database.
    engine = create_engine(
        settings.database_url, pool_size=settings.db_pool_size, pool_pre_ping=True
    )
    session_factory = sessionmaker(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(title="Slot Booking", version=__version__, lifespan=lifespan)

    def get_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    SessionDep = Annotated[Session, Depends(get_session)]

    @app.exception_handler(DomainError)
    def domain_error(_: Request, error: DomainError) -> JSONResponse:
        # Everything not listed explicitly is a conflict with the current state.
        code = _ERROR_STATUS.get(type(error), status.HTTP_409_CONFLICT)
        body = ErrorOut(error=error.code, detail=str(error))
        return JSONResponse(status_code=code, content=body.model_dump())

    @app.get("/health")
    def health() -> Health:
        return Health(
            status="ok",
            service=SERVICE_NAME,
            version=__version__,
            # Set at image build time, so a smoke test can prove which build is running.
            # Missing and empty are the same failure, so both map to UNKNOWN_BUILD.
            build_sha=os.environ.get("SLOT_BUILD_SHA") or UNKNOWN_BUILD,
        )

    @app.post("/slots", status_code=status.HTTP_201_CREATED)
    def create_slot(body: SlotCreate, session: SessionDep) -> SlotOut:
        slot = service.create_slot(
            session, body.master_id, body.starts_at, body.ends_at, now=clock()
        )
        return SlotOut(
            id=slot.id,
            master_id=slot.master_id,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            available=True,
        )

    @app.get("/slots")
    def list_slots(
        master_id: str,
        session: SessionDep,
        day: Annotated[date | None, Query(alias="date", description="UTC day")] = None,
    ) -> list[SlotOut]:
        return [
            SlotOut(
                id=slot.id,
                master_id=slot.master_id,
                starts_at=slot.starts_at,
                ends_at=slot.ends_at,
                available=available,
            )
            for slot, available in service.list_slots(session, master_id, day, now=clock())
        ]

    @app.post("/bookings", status_code=status.HTTP_201_CREATED)
    def create_booking(body: BookingCreate, session: SessionDep) -> BookingOut:
        booking = service.book_slot(session, body.slot_id, body.client_id, now=clock())
        return BookingOut.model_validate(booking)

    @app.get("/bookings/{booking_id}")
    def get_booking(booking_id: uuid.UUID, session: SessionDep) -> BookingOut:
        return BookingOut.model_validate(service.get_booking(session, booking_id))

    @app.delete("/bookings/{booking_id}")
    def cancel_booking(booking_id: uuid.UUID, session: SessionDep) -> BookingOut:
        booking = service.change_status(session, booking_id, BookingStatus.CANCELLED, now=clock())
        return BookingOut.model_validate(booking)

    # Called by the payments service once it exists (iteration 2).
    @app.post("/internal/bookings/{booking_id}/confirm")
    def confirm_booking(booking_id: uuid.UUID, session: SessionDep) -> BookingOut:
        booking = service.change_status(session, booking_id, BookingStatus.CONFIRMED, now=clock())
        return BookingOut.model_validate(booking)

    return app


app = create_app()
