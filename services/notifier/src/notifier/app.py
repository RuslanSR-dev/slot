"""Run with `uvicorn notifier.app:create_app --factory`.

The API is read-only: it answers "what was the client told about this
booking?". Sending happens in the worker process (`python -m
notifier.consumer`), because an HTTP server and a queue consumer have
different reasons to restart.
"""

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from notifier import __version__, service
from notifier.domain import DomainError
from notifier.schemas import ErrorOut, ExternalId, NotificationOut

SERVICE_NAME = "notifier"
UNKNOWN_BUILD = "unknown"


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
            database_url=os.environ["SLOT_DATABASE_URL"],
            db_pool_size=int(os.environ.get("SLOT_DB_POOL_SIZE", "10")),
        )


def errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    """Document error responses in the OpenAPI schema: they are part of the contract."""
    return {code: {"model": ErrorOut} for code in codes}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = create_engine(
        settings.database_url, pool_size=settings.db_pool_size, pool_pre_ping=True
    )
    session_factory = sessionmaker(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(title="Slot Notifier", version=__version__, lifespan=lifespan)

    def get_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    SessionDep = Annotated[Session, Depends(get_session)]

    @app.exception_handler(DomainError)
    def domain_error(_: Request, error: DomainError) -> JSONResponse:
        body = ErrorOut(error=error.code, detail=str(error))
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=body.model_dump())

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

    @app.get("/notifications", responses=errors(422))
    def list_notifications(booking_id: ExternalId, session: SessionDep) -> list[NotificationOut]:
        return [
            NotificationOut.model_validate(notification)
            for notification in service.list_notifications(session, booking_id)
        ]

    return app
