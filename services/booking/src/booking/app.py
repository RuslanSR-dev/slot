import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from booking import __version__, service
from booking.domain import (
    DEFAULT_PENDING_TTL,
    BookingNotFoundError,
    BookingStatus,
    DomainError,
    InvalidSlotError,
    PaymentsUnavailableError,
    SlotNotFoundError,
)
from booking.payments_client import PaymentsClient
from booking.schemas import (
    BookingCreate,
    BookingOut,
    ErrorOut,
    ExternalId,
    PaymentOut,
    SlotCreate,
    SlotOut,
)

SERVICE_NAME = "booking"
# Reported when the build did not say which commit it is. Deliberately loud:
# a plausible-looking default like "dev" would hide a broken pipeline.
UNKNOWN_BUILD = "unknown"

Clock = Callable[[], datetime]

_ERROR_STATUS: dict[type[DomainError], int] = {
    SlotNotFoundError: status.HTTP_404_NOT_FOUND,
    BookingNotFoundError: status.HTTP_404_NOT_FOUND,
    InvalidSlotError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PaymentsUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}


class Health(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    build_sha: str


@dataclass(frozen=True)
class Settings:
    database_url: str
    payments_url: str = "http://localhost:8001"
    payments_timeout: float = 5.0
    db_pool_size: int = 10
    pending_ttl: timedelta = DEFAULT_PENDING_TTL

    @classmethod
    def from_env(cls) -> Settings:
        ttl_seconds = os.environ.get("SLOT_PENDING_TTL_SECONDS")
        return cls(
            database_url=os.environ.get(
                "SLOT_DATABASE_URL", "postgresql+psycopg://slot:slot@localhost:5432/slot"
            ),
            payments_url=os.environ.get("SLOT_PAYMENTS_URL", cls.payments_url),
            db_pool_size=int(os.environ.get("SLOT_DB_POOL_SIZE", "10")),
            pending_ttl=timedelta(seconds=int(ttl_seconds)) if ttl_seconds else DEFAULT_PENDING_TTL,
        )


def system_clock() -> datetime:
    return datetime.now(UTC)


def errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    """Document error responses in the OpenAPI schema: they are part of the contract."""
    return {code: {"model": ErrorOut} for code in codes}


def create_app(settings: Settings | None = None, clock: Clock = system_clock) -> FastAPI:
    settings = settings or Settings.from_env()
    ttl = settings.pending_ttl
    payments = PaymentsClient(settings.payments_url, settings.payments_timeout)
    # Connections are opened lazily: creating the app does not need a database.
    engine = create_engine(
        settings.database_url, pool_size=settings.db_pool_size, pool_pre_ping=True
    )
    session_factory = sessionmaker(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        payments.close()
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

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        """Framework errors (400, 404, 405) in the same shape as ours."""
        headers = dict(error.headers or {})
        if error.status_code == status.HTTP_405_METHOD_NOT_ALLOWED:
            # Starlette lists only the first route matching the path; RFC 9110
            # requires every method the resource supports. Found by fuzzing.
            headers["Allow"] = ", ".join(
                sorted(
                    method
                    for route in app.router.routes
                    if isinstance(route, APIRoute) and route.matches(request.scope)[0] != Match.NONE
                    for method in route.methods or ()
                )
            )
        phrase = HTTPStatus(error.status_code).phrase.lower().replace(" ", "_")
        body = ErrorOut(error=phrase, detail=str(error.detail))
        return JSONResponse(
            status_code=error.status_code, content=body.model_dump(), headers=headers
        )

    @app.exception_handler(RequestValidationError)
    def invalid_request(_: Request, error: RequestValidationError) -> JSONResponse:
        # One error shape for the whole API: clients parse `error`, never FastAPI internals.
        fields = ", ".join(".".join(str(part) for part in e["loc"]) for e in error.errors())
        body = ErrorOut(error="invalid_request", detail=f"invalid: {fields}")
        return JSONResponse(status_code=422, content=body.model_dump())

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

    @app.post("/slots", status_code=status.HTTP_201_CREATED, responses=errors(400, 422))
    def create_slot(body: SlotCreate, session: SessionDep) -> SlotOut:
        slot = service.create_slot(
            session, body.master_id, body.starts_at, body.ends_at, body.price_minor, now=clock()
        )
        return SlotOut(
            id=slot.id,
            master_id=slot.master_id,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            price_minor=slot.price_minor,
            available=True,
        )

    @app.get("/slots", responses=errors(422))
    def list_slots(
        master_id: ExternalId,
        session: SessionDep,
        day: Annotated[date | None, Query(alias="date", description="UTC day")] = None,
    ) -> list[SlotOut]:
        return [
            SlotOut(
                id=slot.id,
                master_id=slot.master_id,
                starts_at=slot.starts_at,
                ends_at=slot.ends_at,
                price_minor=slot.price_minor,
                available=available,
            )
            for slot, available in service.list_slots(session, master_id, day, clock(), ttl)
        ]

    @app.post(
        "/bookings", status_code=status.HTTP_201_CREATED, responses=errors(400, 404, 409, 422)
    )
    def create_booking(body: BookingCreate, session: SessionDep) -> BookingOut:
        booking = service.book_slot(session, body.slot_id, body.client_id, clock(), ttl)
        return BookingOut.model_validate(booking)

    @app.post("/bookings/{booking_id}/payment", responses=errors(404, 409, 422, 503))
    def pay_for_booking(booking_id: uuid.UUID, session: SessionDep) -> PaymentOut:
        _, slot = service.get_payable_booking(session, booking_id, clock(), ttl)
        amount = slot.price_minor
        # Do not hold a database transaction open during a network call.
        session.rollback()
        payment = payments.create_payment(booking_id, amount)
        return PaymentOut(
            payment_id=payment.id, status=payment.status, checkout_url=payment.checkout_url
        )

    @app.get("/bookings/{booking_id}", responses=errors(404, 422))
    def get_booking(booking_id: uuid.UUID, session: SessionDep) -> BookingOut:
        return BookingOut.model_validate(service.get_booking(session, booking_id))

    @app.delete("/bookings/{booking_id}", responses=errors(404, 409, 422))
    def cancel_booking(booking_id: uuid.UUID, session: SessionDep) -> BookingOut:
        booking = service.change_status(session, booking_id, BookingStatus.CANCELLED, now=clock())
        return BookingOut.model_validate(booking)

    # Called by the payments service after a successful charge. Idempotent (ADR-0008).
    @app.post("/internal/bookings/{booking_id}/confirm", responses=errors(404, 409, 422))
    def confirm_booking(booking_id: uuid.UUID, session: SessionDep) -> BookingOut:
        booking = service.change_status(session, booking_id, BookingStatus.CONFIRMED, now=clock())
        return BookingOut.model_validate(booking)

    return app


app = create_app()
