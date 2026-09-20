"""Run with `uvicorn payments.app:create_app --factory`.

No module-level app: settings are read when the server starts, and a missing
webhook secret stops the start instead of silently accepting any webhook.
"""

import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from payments import __version__, service
from payments.domain import (
    DomainError,
    InvalidEventError,
    InvalidSignatureError,
    PaymentNotFoundError,
    ProviderUnavailableError,
    verify_signature,
)
from payments.paystub import PayStubClient
from payments.schemas import ErrorOut, PaymentCreate, PaymentOut, WebhookAck, WebhookEvent

SERVICE_NAME = "payments"
UNKNOWN_BUILD = "unknown"

Clock = Callable[[], datetime]

_ERROR_STATUS: dict[type[DomainError], int] = {
    InvalidSignatureError: status.HTTP_401_UNAUTHORIZED,
    InvalidEventError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PaymentNotFoundError: status.HTTP_404_NOT_FOUND,
    ProviderUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}


class Health(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    build_sha: str


@dataclass(frozen=True)
class Settings:
    database_url: str
    paystub_url: str
    paystub_api_key: str
    webhook_secret: str
    booking_url: str
    http_timeout: float = 5.0
    db_pool_size: int = 10

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            database_url=env["SLOT_DATABASE_URL"],
            paystub_url=env["SLOT_PAYSTUB_URL"],
            paystub_api_key=env["SLOT_PAYSTUB_API_KEY"],
            webhook_secret=env["SLOT_PAYSTUB_WEBHOOK_SECRET"],
            booking_url=env["SLOT_BOOKING_URL"],
            db_pool_size=int(env.get("SLOT_DB_POOL_SIZE", "10")),
        )


def system_clock() -> datetime:
    return datetime.now(UTC)


def errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    """Document error responses in the OpenAPI schema: they are part of the contract."""
    return {code: {"model": ErrorOut} for code in codes}


async def raw_body(request: Request) -> bytes:
    """The exact bytes PayStub signed. A re-serialized JSON would not match."""
    return await request.body()


def create_app(settings: Settings | None = None, clock: Clock = system_clock) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = create_engine(
        settings.database_url, pool_size=settings.db_pool_size, pool_pre_ping=True
    )
    session_factory = sessionmaker(engine)
    paystub = PayStubClient(settings.paystub_url, settings.paystub_api_key, settings.http_timeout)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        paystub.close()
        engine.dispose()

    app = FastAPI(title="Slot Payments", version=__version__, lifespan=lifespan)

    def get_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    SessionDep = Annotated[Session, Depends(get_session)]

    @app.exception_handler(DomainError)
    def domain_error(_: Request, error: DomainError) -> JSONResponse:
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
            build_sha=os.environ.get("SLOT_BUILD_SHA") or UNKNOWN_BUILD,
        )

    @app.post(
        "/payments",
        status_code=status.HTTP_201_CREATED,
        responses={
            # A repeat for the same booking returns the existing payment (ADR-0008).
            # Found undocumented by fuzzing.
            200: {"model": PaymentOut, "description": "The payment already existed"},
            **errors(400, 409, 422, 503),
        },
    )
    def create_payment(body: PaymentCreate, session: SessionDep, response: Response) -> PaymentOut:
        payment, created = service.create_payment(
            session, paystub, body.booking_id, body.amount_minor, body.currency, now=clock()
        )
        if not created:
            response.status_code = status.HTTP_200_OK
        return PaymentOut.model_validate(payment)

    @app.get("/payments/{payment_id}", responses=errors(404, 422))
    def get_payment(payment_id: uuid.UUID, session: SessionDep) -> PaymentOut:
        return PaymentOut.model_validate(service.get_payment(session, payment_id))

    # No 503 any more: since ADR-0010 nothing here waits for a neighbour, so
    # there is no failure a retry by PayStub could fix.
    @app.post("/webhooks/paystub", responses=errors(401, 422))
    def paystub_webhook(
        body: Annotated[bytes, Depends(raw_body)],
        session: SessionDep,
        signature: Annotated[str | None, Header(alias="PayStub-Signature")] = None,
    ) -> WebhookAck:
        # Check the signature before parsing: an unsigned body is not trusted at all.
        verify_signature(body, signature, settings.webhook_secret)
        try:
            event = WebhookEvent.model_validate_json(body)
        except ValidationError as error:
            raise InvalidEventError(f"not a PayStub event: {error.error_count()} errors") from error
        outcome = service.handle_webhook(
            session, event.id, event.type, event.data.reference, now=clock()
        )
        return WebhookAck(outcome=outcome)

    return app
