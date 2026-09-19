"""What payments needs from booking: the consumer side of the contract (ADR-0007).

Result: contracts/pacts/payments-booking.json, verified by booking in its CI.
payments reads only the status code of the confirmation, so nothing in the
body is pinned: booking may change its response body freely.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from pact import Pact

from payments.booking_client import BookingClient
from payments.domain import BookingRejectedError

PACT_DIR = Path(__file__).resolve().parents[4] / "contracts" / "pacts"
PACT_FILE = PACT_DIR / "payments-booking.json"
BOOKING_ID = uuid.UUID("01999999-0000-7000-8000-000000000002")
CONFIRM = f"/internal/bookings/{BOOKING_ID}/confirm"


@pytest.fixture(scope="module", autouse=True)
def fresh_pact_file() -> None:
    """Start from nothing: an interaction removed from the tests leaves the contract."""
    PACT_FILE.unlink(missing_ok=True)


@pytest.fixture
def pact() -> Iterator[Pact]:
    # One Pact per test: after its mock server has run, a Pact cannot be changed.
    pact = Pact("payments", "booking").with_specification("V4")
    yield pact
    pact.write_file(PACT_DIR)  # merges into the file written by the previous tests


@contextmanager
def booking_client(pact: Pact) -> Iterator[BookingClient]:
    with pact.serve() as server:
        client = BookingClient(str(server.url))
        try:
            yield client
        finally:
            client.close()


def expect_confirmation(pact: Pact, description: str, state: str, status: int) -> None:
    (
        pact.upon_receiving(description, "HTTP")
        .given(state, booking_id=str(BOOKING_ID))
        .with_request("POST", CONFIRM)
        .will_respond_with(status)
    )


def test_pending_booking_is_confirmed(pact: Pact) -> None:
    expect_confirmation(pact, "a confirmation of a paid booking", "a pending booking exists", 200)

    with booking_client(pact) as client:
        client.confirm(BOOKING_ID)


def test_confirming_again_is_a_success(pact: Pact) -> None:
    """payments retries after a lost answer; ADR-0008 relies on this being 200."""
    expect_confirmation(
        pact, "a repeated confirmation of a paid booking", "a confirmed booking exists", 200
    )

    with booking_client(pact) as client:
        client.confirm(BOOKING_ID)


def test_expired_booking_rejects_the_confirmation(pact: Pact) -> None:
    expect_confirmation(
        pact,
        "a confirmation that arrives after the booking expired",
        "an expired booking exists",
        409,
    )

    with booking_client(pact) as client, pytest.raises(BookingRejectedError):
        client.confirm(BOOKING_ID)
