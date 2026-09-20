"""HTTP client of the booking service. Web knows nothing else about it.

Every call carries the token of the person who is looking at the page, so
the rules are decided by booking. The web service never asks "may this
person do it?" - it could only get that answer wrong.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, TypeVar

import httpx2

TIMEOUT = 5.0
# A booking answer we cannot read is still an answer: never a traceback on a page.
UNREADABLE = "unreadable_answer"
UNAVAILABLE = "booking_unavailable"

T = TypeVar("T")


class BookingError(Exception):
    """What booking refused and why. `code` is its error code, not our text."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(f"booking answered {status}: {code}")
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Slot:
    id: uuid.UUID
    master_id: str
    starts_at: datetime
    ends_at: datetime
    price_minor: int
    available: bool


@dataclass(frozen=True)
class Booking:
    id: uuid.UUID
    slot_id: uuid.UUID
    client_id: str
    master_id: str
    status: str
    starts_at: datetime
    ends_at: datetime
    price_minor: int


@dataclass(frozen=True)
class Payment:
    payment_id: uuid.UUID
    status: str
    checkout_url: str | None


def _payment(row: dict[str, Any]) -> Payment:
    return Payment(
        payment_id=uuid.UUID(row["payment_id"]),
        status=row["status"],
        checkout_url=row["checkout_url"],
    )


def _slot(row: dict[str, Any]) -> Slot:
    return Slot(
        id=uuid.UUID(row["id"]),
        master_id=row["master_id"],
        starts_at=datetime.fromisoformat(row["starts_at"]),
        ends_at=datetime.fromisoformat(row["ends_at"]),
        price_minor=row["price_minor"],
        available=row["available"],
    )


def _booking(row: dict[str, Any]) -> Booking:
    return Booking(
        id=uuid.UUID(row["id"]),
        slot_id=uuid.UUID(row["slot_id"]),
        client_id=row["client_id"],
        master_id=row["master_id"],
        status=row["status"],
        starts_at=datetime.fromisoformat(row["starts_at"]),
        ends_at=datetime.fromisoformat(row["ends_at"]),
        price_minor=row["price_minor"],
    )


class BookingClient:
    def __init__(self, base_url: str, timeout: float = TIMEOUT) -> None:
        self._http = httpx2.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def _call(
        self,
        method: str,
        path: str,
        token: str | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = self._http.request(method, path, headers=headers, json=json, params=params)
        except httpx2.HTTPError as error:
            # A neighbour that does not answer is a page that says so, not a 500.
            raise BookingError(UNAVAILABLE, 503) from error
        if response.is_success:
            return response.json()
        try:
            code = str(response.json()["error"])
        except ValueError, KeyError, TypeError:
            code = UNREADABLE
        raise BookingError(code, response.status_code)

    @staticmethod
    def _read(build: Callable[[Any], T], payload: Any) -> T:
        """An answer we cannot read is a refusal, not a traceback on a page.

        Found by a component test: a booking that answers 200 with something
        unexpected used to reach the template and end as a 500.
        """
        try:
            return build(payload)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise BookingError(UNREADABLE, 502) from error

    def list_slots(self, master_id: str, day: date | None = None) -> list[Slot]:
        params: dict[str, Any] = {"master_id": master_id}
        if day is not None:
            params["date"] = day.isoformat()
        answer = self._call("GET", "/slots", params=params)
        return self._read(lambda rows: [_slot(row) for row in rows], answer)

    def create_slot(
        self, token: str, starts_at: datetime, ends_at: datetime, price_minor: int
    ) -> Slot:
        body = {
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "price_minor": price_minor,
        }
        return self._read(_slot, self._call("POST", "/slots", token=token, json=body))

    def create_booking(self, token: str, slot_id: uuid.UUID) -> uuid.UUID:
        created = self._call("POST", "/bookings", token=token, json={"slot_id": str(slot_id)})
        return self._read(lambda row: uuid.UUID(row["id"]), created)

    def my_bookings(self, token: str) -> list[Booking]:
        answer = self._call("GET", "/bookings", token=token)
        return self._read(lambda rows: [_booking(row) for row in rows], answer)

    def cancel_booking(self, token: str, booking_id: uuid.UUID) -> str:
        cancelled = self._call("DELETE", f"/bookings/{booking_id}", token=token)
        return self._read(lambda row: str(row["status"]), cancelled)

    def booking_status(self, token: str, booking_id: uuid.UUID) -> str:
        booking = self._call("GET", f"/bookings/{booking_id}", token=token)
        return self._read(lambda row: str(row["status"]), booking)

    def start_payment(self, token: str, booking_id: uuid.UUID) -> Payment:
        paid = self._call("POST", f"/bookings/{booking_id}/payment", token=token)
        return self._read(_payment, paid)
