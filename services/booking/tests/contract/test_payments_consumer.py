"""What booking needs from payments: the consumer side of the contract (ADR-0007).

Each test runs the real PaymentsClient against a Pact mock server and records
the interaction. The result, contracts/pacts/booking-payments.json, is
verified by payments in its own CI. Only fields booking actually reads are
pinned; payments may add or change anything else.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from pact import Pact, match
from pact.interaction import HttpInteraction

from booking.domain import PaymentConflictError, PaymentsUnavailableError
from booking.payments_client import CURRENCY, PaymentsClient

PACT_DIR = Path(__file__).resolve().parents[4] / "contracts" / "pacts"
BOOKING_ID = uuid.UUID("01999999-0000-7000-8000-000000000001")
AMOUNT = 150_000
REQUEST = {"booking_id": str(BOOKING_ID), "amount_minor": AMOUNT, "currency": CURRENCY}
PAYMENT = {
    "id": match.uuid(),
    "status": match.str("pending"),
    "checkout_url": match.str("https://paystub.example/checkout/ch_1"),
}


PACT_FILE = PACT_DIR / "booking-payments.json"


@pytest.fixture(scope="module", autouse=True)
def fresh_pact_file() -> None:
    """Start from nothing: an interaction removed from the tests leaves the contract."""
    PACT_FILE.unlink(missing_ok=True)


@pytest.fixture
def pact() -> Iterator[Pact]:
    # One Pact per test: after its mock server has run, a Pact cannot be changed.
    pact = Pact("booking", "payments").with_specification("V4")
    yield pact
    pact.write_file(PACT_DIR)  # merges into the file written by the previous tests


@contextmanager
def payments_client(pact: Pact) -> Iterator[PaymentsClient]:
    """The real client, pointed at the Pact mock server; closed afterwards."""
    with pact.serve() as server:
        client = PaymentsClient(str(server.url))
        try:
            yield client
        finally:
            client.close()


def create_payment_request(
    pact: Pact, description: str, state: str, **params: object
) -> HttpInteraction:
    return (
        pact.upon_receiving(description, "HTTP")
        .given(state, **params)
        .with_request("POST", "/payments")
        .with_body(REQUEST, content_type="application/json")
    )


def test_payment_is_created(pact: Pact) -> None:
    create_payment_request(
        pact, "a request to pay for a booking", "the payment provider accepts charges"
    ).will_respond_with(201).with_body(PAYMENT, content_type="application/json")

    with payments_client(pact) as client:
        payment = client.create_payment(BOOKING_ID, AMOUNT)

    assert payment.status == "pending"
    assert payment.checkout_url


def test_repeated_request_returns_the_existing_payment(pact: Pact) -> None:
    """booking retries after a lost answer and relies on getting the payment back."""
    create_payment_request(
        pact,
        "a repeated request to pay for a booking",
        "a payment exists for the booking",
        booking_id=str(BOOKING_ID),
        amount_minor=AMOUNT,
    ).will_respond_with(200).with_body(PAYMENT, content_type="application/json")

    with payments_client(pact) as client:
        payment = client.create_payment(BOOKING_ID, AMOUNT)

    assert payment.status == "pending"


def test_payment_with_other_terms_is_a_conflict(pact: Pact) -> None:
    create_payment_request(
        pact,
        "a request to pay a different amount for an already paid booking",
        "a payment exists for the booking",
        booking_id=str(BOOKING_ID),
        amount_minor=99_000,
    ).will_respond_with(409)

    with payments_client(pact) as client, pytest.raises(PaymentConflictError):
        client.create_payment(BOOKING_ID, AMOUNT)


def test_provider_outage_is_reported_as_unavailable(pact: Pact) -> None:
    create_payment_request(
        pact, "a request to pay while the provider is down", "the payment provider is down"
    ).will_respond_with(503)

    with payments_client(pact) as client, pytest.raises(PaymentsUnavailableError):
        client.create_payment(BOOKING_ID, AMOUNT)
