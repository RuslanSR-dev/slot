"""Booking asks payments to create a payment. Payments is a WireMock stub here:
the test decides whether it answers, fails, hangs or drops the connection."""

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import NOW, PAYMENTS_TIMEOUT, PENDING_TTL, FixedClock
from .test_bookings_api import PRICE, book, create_slot
from .wiremock import WireMock

PAYMENT_ID = str(uuid.UUID(int=1))


@pytest.fixture
def booking(client: TestClient) -> Any:
    slot = create_slot(client)
    return book(client, slot["id"]).json()


def pay(client: TestClient, booking_id: str) -> Any:
    return client.post(f"/bookings/{booking_id}/payment")


def test_payment_is_created_for_the_slot_price(
    client: TestClient, payments_stub: WireMock, booking: Any
) -> None:
    payments_stub.stub(
        "POST",
        "/payments",
        status=201,
        json_body={"id": PAYMENT_ID, "status": "pending", "checkout_url": "https://pay/1"},
    )

    response = pay(client, booking["id"])

    assert response.status_code == 200
    assert response.json() == {
        "payment_id": PAYMENT_ID,
        "status": "pending",
        "checkout_url": "https://pay/1",
    }
    [sent] = payments_stub.received("POST", "/payments")
    assert json.loads(sent["body"]) == {
        "booking_id": booking["id"],
        "amount_minor": PRICE,
        "currency": "RUB",
    }


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"status": 500}, id="server-error"),
        pytest.param({"status": 200, "delay_ms": int(PAYMENTS_TIMEOUT * 1000) + 500}, id="hangs"),
        pytest.param({"fault": "CONNECTION_RESET_BY_PEER"}, id="connection-reset"),
        pytest.param({"fault": "EMPTY_RESPONSE"}, id="empty-response"),
    ],
)
def test_payments_failure_is_reported_as_unavailable(
    client: TestClient, payments_stub: WireMock, booking: Any, failure: dict[str, Any]
) -> None:
    payments_stub.stub("POST", "/payments", **failure)

    response = pay(client, booking["id"])

    assert response.status_code == 503
    assert response.json()["error"] == "payments_unavailable"
    # The booking is untouched: the client can retry the payment.
    assert client.get(f"/bookings/{booking['id']}").json()["status"] == "pending"


def test_payment_conflict_is_passed_on(
    client: TestClient, payments_stub: WireMock, booking: Any
) -> None:
    payments_stub.stub("POST", "/payments", status=409, json_body={"error": "payment_conflict"})

    response = pay(client, booking["id"])

    assert response.status_code == 409
    assert response.json()["error"] == "payment_conflict"


class TestNotPayable:
    """Payments must not even be called for a booking that cannot be paid."""

    def test_cancelled_booking(
        self, client: TestClient, payments_stub: WireMock, booking: Any
    ) -> None:
        client.delete(f"/bookings/{booking['id']}")

        response = pay(client, booking["id"])

        assert response.status_code == 409
        assert response.json()["error"] == "booking_not_payable"
        assert payments_stub.received("POST", "/payments") == []

    def test_booking_past_its_ttl(
        self, client: TestClient, payments_stub: WireMock, booking: Any, clock: FixedClock
    ) -> None:
        clock.now = NOW + PENDING_TTL

        response = pay(client, booking["id"])

        assert response.status_code == 409
        assert response.json()["error"] == "booking_not_payable"
        assert payments_stub.received("POST", "/payments") == []

    def test_booking_just_before_its_ttl_is_still_payable(
        self, client: TestClient, payments_stub: WireMock, booking: Any, clock: FixedClock
    ) -> None:
        payments_stub.stub(
            "POST",
            "/payments",
            status=201,
            json_body={"id": PAYMENT_ID, "status": "pending", "checkout_url": None},
        )
        clock.now = NOW + PENDING_TTL - timedelta(seconds=1)

        assert pay(client, booking["id"]).status_code == 200

    def test_unknown_booking(self, client: TestClient, payments_stub: WireMock) -> None:
        response = pay(client, "00000000-0000-7000-8000-000000000000")

        assert response.status_code == 404
        assert payments_stub.received("POST", "/payments") == []
