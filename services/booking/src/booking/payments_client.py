"""HTTP client of the payments service. Booking knows nothing else about it."""

import uuid
from dataclasses import dataclass

import httpx2

from booking.domain import PaymentConflictError, PaymentsUnavailableError

CURRENCY = "RUB"


@dataclass(frozen=True)
class Payment:
    id: uuid.UUID
    status: str
    checkout_url: str | None


class PaymentsClient:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self._http = httpx2.Client(base_url=base_url, timeout=timeout)

    def create_payment(self, booking_id: uuid.UUID, amount_minor: int) -> Payment:
        try:
            response = self._http.post(
                "/payments",
                json={
                    "booking_id": str(booking_id),
                    "amount_minor": amount_minor,
                    "currency": CURRENCY,
                },
            )
        except httpx2.HTTPError as error:
            raise PaymentsUnavailableError(f"payments did not respond: {error}") from error

        if response.status_code == 409:
            raise PaymentConflictError(response.text)
        if response.status_code not in (200, 201):
            raise PaymentsUnavailableError(f"payments answered {response.status_code}")
        body = response.json()
        return Payment(
            id=uuid.UUID(body["id"]), status=body["status"], checkout_url=body["checkout_url"]
        )

    def close(self) -> None:
        self._http.close()
