"""HTTP client of the booking service. Payments knows nothing else about it."""

import uuid

import httpx2

from payments.domain import BookingRejectedError, BookingUnavailableError


class BookingClient:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self._http = httpx2.Client(base_url=base_url, timeout=timeout)

    def confirm(self, booking_id: uuid.UUID) -> None:
        """Confirm a paid booking. Safe to repeat: booking treats a repeat as success."""
        try:
            response = self._http.post(f"/internal/bookings/{booking_id}/confirm")
        except httpx2.HTTPError as error:
            raise BookingUnavailableError(f"booking did not respond: {error}") from error
        if response.status_code == 409:
            # The booking expired or was cancelled before the money arrived.
            raise BookingRejectedError(response.text)
        if response.status_code != 200:
            raise BookingUnavailableError(f"booking answered {response.status_code}")

    def close(self) -> None:
        self._http.close()
