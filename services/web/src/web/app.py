"""The pages people use. Run with `uvicorn web.app:create_app --factory`.

Server-rendered templates plus HTMX: the server answers with HTML, and the
page swaps a piece of itself. There is no build step, no bundle and no Node
in the repository - one person maintains this, and the value of the project
is in the quality infrastructure around it, not in a single-page app.

Web owns no data and no rules. It holds the token of the person looking at
the page and passes it on; booking decides what that person may do. If
booking says 401, the session is over, and the only thing web does about
it is show the login page again.
"""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Cookie, Depends, FastAPI, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from web import __version__
from web.booking_client import BookingClient, BookingError
from web.domain import (
    SESSION_LIFETIME,
    InvalidInputError,
    Role,
    clean_name,
    explain,
    issue_token,
    parse_role,
    price_to_minor,
    price_to_roubles,
    to_utc,
)

SERVICE_NAME = "web"
UNKNOWN_BUILD = "unknown"
SESSION_COOKIE = "slot_session"
ROLE_COOKIE = "slot_role"
NAME_COOKIE = "slot_name"
COOKIES = (SESSION_COOKIE, NAME_COOKIE, ROLE_COOKIE)
HERE = Path(__file__).parent
# The one status a page keeps asking about; everything else is final.
PENDING = "pending"
SESSION_OVER = "Сессия истекла, войдите заново"


class Health(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    build_sha: str


@dataclass(frozen=True)
class Settings:
    booking_url: str
    # The same secret booking verifies with. Sharing it is the whole of our
    # authentication - see docs for what that deliberately does not cover.
    auth_secret: str
    booking_timeout: float = 5.0

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            booking_url=os.environ.get("SLOT_BOOKING_URL", "http://localhost:8000"),
            auth_secret=os.environ["SLOT_AUTH_SECRET"],
        )


@dataclass(frozen=True)
class Visitor:
    """Who the browser says it is. The proof is the token, and only booking reads it."""

    token: str
    name: str
    role: Role


def current_visitor(
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    name: Annotated[str | None, Cookie(alias=NAME_COOKIE)] = None,
    role: Annotated[str | None, Cookie(alias=ROLE_COOKIE)] = None,
) -> Visitor | None:
    if not token or not name or not role:
        return None
    try:
        return Visitor(token=token, name=name, role=parse_role(role))
    except InvalidInputError:
        return None


VisitorDep = Annotated[Visitor | None, Depends(current_visitor)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    booking = BookingClient(settings.booking_url, settings.booking_timeout)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        booking.close()

    app = FastAPI(title="Slot Web", version=__version__, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["roubles"] = price_to_roubles

    def render(request: Request, name: str, context: dict[str, object]) -> Response:
        """A page or a piece of one: HTMX swaps the same HTML the page is made of."""
        return templates.TemplateResponse(request, name, context)

    def login_page(request: Request, message: str | None = None, code: int = 200) -> Response:
        response = templates.TemplateResponse(
            request, "login.html", {"message": message}, status_code=code
        )
        # Whatever was in the cookies is not worth anything any more.
        for cookie in COOKIES:
            response.delete_cookie(cookie)
        return response

    def problem(request: Request, error: BookingError) -> Response:
        """A refusal from booking, turned into a sentence on the page.

        The status stays 200 on purpose: HTMX swaps only successful answers,
        and a person must see the reason instead of an empty box.
        """
        return render(request, "partials/problem.html", {"message": explain(error.code)})

    def status_row(request: Request, booking_id: uuid.UUID, state: str) -> Response:
        """The row of a booking. While it is pending, it comes back asking for itself."""
        return render(
            request,
            "partials/booking_status.html",
            {"booking_id": booking_id, "state": state, "polling": state == PENDING},
        )

    @app.get("/health")
    def health() -> Health:
        return Health(
            status="ok",
            service=SERVICE_NAME,
            version=__version__,
            build_sha=os.environ.get("SLOT_BUILD_SHA") or UNKNOWN_BUILD,
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, who: VisitorDep) -> Response:
        if who is None:
            return login_page(request)
        return RedirectResponse(_home(who), status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/login", response_class=HTMLResponse)
    def login(
        request: Request,
        name: Annotated[str, Form()] = "",
        role: Annotated[str, Form()] = "",
    ) -> Response:
        """Logging in by name only: no password is asked for and none is stored."""
        try:
            subject, known_role = clean_name(name), parse_role(role)
        except InvalidInputError as error:
            return login_page(request, str(error), code=status.HTTP_400_BAD_REQUEST)
        who = Visitor(
            token=issue_token(subject, known_role, settings.auth_secret, datetime.now(UTC)),
            name=subject,
            role=known_role,
        )
        response = RedirectResponse(_home(who), status_code=status.HTTP_303_SEE_OTHER)
        lifetime = int(SESSION_LIFETIME.total_seconds())
        # httponly: a script on the page has no business reading the token.
        response.set_cookie(
            SESSION_COOKIE, who.token, max_age=lifetime, httponly=True, samesite="lax"
        )
        response.set_cookie(NAME_COOKIE, subject, max_age=lifetime, samesite="lax")
        response.set_cookie(ROLE_COOKIE, str(known_role), max_age=lifetime, samesite="lax")
        return response

    @app.post("/logout", response_class=HTMLResponse)
    def logout(request: Request) -> Response:
        return login_page(request, "Вы вышли")

    @app.get("/bookings", response_class=HTMLResponse)
    def my_bookings(request: Request, who: VisitorDep) -> Response:
        if who is None:
            return login_page(request)
        try:
            bookings = booking.my_bookings(who.token)
        except BookingError as error:
            if error.status == status.HTTP_401_UNAUTHORIZED:
                return login_page(request, SESSION_OVER)
            return render(
                request,
                "bookings.html",
                {"who": who, "bookings": [], "message": explain(error.code)},
            )
        return render(request, "bookings.html", {"who": who, "bookings": bookings, "message": None})

    @app.get("/masters/{master_id}", response_class=HTMLResponse)
    def master_slots(request: Request, master_id: str, who: VisitorDep) -> Response:
        if who is None:
            return login_page(request)
        try:
            slots = booking.list_slots(master_id)
            message = None
        except BookingError as error:
            slots, message = [], explain(error.code)
        return render(
            request,
            "slots.html",
            {"who": who, "master_id": master_id, "slots": slots, "message": message},
        )

    @app.post("/slots/{slot_id}/book", response_class=HTMLResponse)
    def book(request: Request, slot_id: uuid.UUID, who: VisitorDep) -> Response:
        if who is None:
            return login_page(request)
        try:
            booking.create_booking(who.token, slot_id)
        except BookingError as error:
            return problem(request, error)
        return render(request, "partials/booked.html", {})

    @app.get("/my-slots", response_class=HTMLResponse)
    def my_slots(request: Request, who: VisitorDep) -> Response:
        if who is None:
            return login_page(request)
        try:
            slots = booking.list_slots(who.name)
            message = None
        except BookingError as error:
            slots, message = [], explain(error.code)
        return render(request, "my_slots.html", {"who": who, "slots": slots, "message": message})

    @app.post("/my-slots", response_class=HTMLResponse)
    def publish_slot(
        request: Request,
        who: VisitorDep,
        starts_at: Annotated[str, Form()],
        duration_minutes: Annotated[int, Form()],
        price: Annotated[str, Form()],
        timezone_offset: Annotated[int, Form()],
    ) -> Response:
        """The form sends the time on the master's own clock, plus the offset.

        Slot keeps UTC. Converting here, and not in the browser, leaves one
        place where this can go wrong - and that place has unit tests and an
        e2e test that runs in a timezone that is not UTC.
        """
        if who is None:
            return login_page(request)
        try:
            starts = to_utc(datetime.fromisoformat(starts_at), timezone_offset)
            slot = booking.create_slot(
                who.token,
                starts,
                starts + timedelta(minutes=duration_minutes),
                price_to_minor(price),
            )
        except (InvalidInputError, ValueError) as error:
            return render(request, "partials/problem.html", {"message": str(error)})
        except BookingError as error:
            return problem(request, error)
        return render(request, "partials/slot_row.html", {"slot": slot})

    @app.get("/bookings/{booking_id}/row", response_class=HTMLResponse)
    def booking_row(request: Request, booking_id: uuid.UUID, who: VisitorDep) -> Response:
        """One row, asked for again while the booking is still pending.

        The polling lives in the answer: a pending booking comes back with
        an hx-trigger, a settled one comes back without it. That is how the
        page stops asking, and how a test can see that it stopped.
        """
        if who is None:
            return login_page(request)
        try:
            return status_row(request, booking_id, booking.booking_status(who.token, booking_id))
        except BookingError as error:
            return problem(request, error)

    @app.post("/bookings/{booking_id}/cancel", response_class=HTMLResponse)
    def cancel(request: Request, booking_id: uuid.UUID, who: VisitorDep) -> Response:
        if who is None:
            return login_page(request)
        try:
            return status_row(request, booking_id, booking.cancel_booking(who.token, booking_id))
        except BookingError as error:
            return problem(request, error)

    @app.post("/bookings/{booking_id}/pay", response_class=HTMLResponse)
    def pay(request: Request, booking_id: uuid.UUID, who: VisitorDep) -> Response:
        """Hand the person over to the payment provider.

        A plain form, not HTMX: this is a real navigation to a page we do
        not own, and coming back from it is a real navigation too.
        """
        if who is None:
            return login_page(request)
        try:
            payment = booking.start_payment(who.token, booking_id)
        except BookingError as error:
            return render(request, "problem.html", {"who": who, "message": explain(error.code)})
        if not payment.checkout_url:
            return render(
                request,
                "problem.html",
                {"who": who, "message": explain("payments_unavailable")},
            )
        return RedirectResponse(payment.checkout_url, status_code=status.HTTP_303_SEE_OTHER)

    return app


def _home(who: Visitor) -> str:
    return "/bookings" if who.role is Role.CLIENT else "/my-slots"
