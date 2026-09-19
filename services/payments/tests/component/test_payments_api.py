import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from .conftest import (
    CHARGES,
    HTTP_TIMEOUT,
    PAYSTUB_API_KEY,
    create_payment,
    stub_charge_created,
)
from .wiremock import WireMock

BOOKING = uuid.UUID(int=42)


def payments_in_database(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM payments")).scalar_one())


def test_payment_is_created_with_a_provider_charge(client: TestClient, stubs: WireMock) -> None:
    stub_charge_created(stubs, "ch_1")

    response = create_payment(client, BOOKING)

    assert response.status_code == 201
    payment = response.json()
    assert payment["status"] == "pending"
    assert payment["checkout_url"] == "https://paystub.example/checkout/ch_1"
    [sent] = stubs.received("POST", CHARGES)
    assert json.loads(sent["body"]) == {
        "amount": 150_000,
        "currency": "RUB",
        "reference": payment["id"],
    }
    assert sent["headers"]["Idempotency-Key"] == payment["id"]
    assert sent["headers"]["Authorization"] == f"Bearer {PAYSTUB_API_KEY}"


class TestRepeatedRequest:
    """booking retries when it did not get the answer (ADR-0008)."""

    def test_repeat_returns_the_same_payment_without_a_second_charge(
        self, client: TestClient, stubs: WireMock, engine: Engine
    ) -> None:
        stub_charge_created(stubs)
        first = create_payment(client, BOOKING)

        second = create_payment(client, BOOKING)

        assert second.status_code == 200, "an existing payment is not created again"
        assert second.json() == first.json()
        assert len(stubs.received("POST", CHARGES)) == 1
        assert payments_in_database(engine) == 1

    def test_repeat_with_another_amount_is_a_conflict(
        self, client: TestClient, stubs: WireMock
    ) -> None:
        stub_charge_created(stubs)
        create_payment(client, BOOKING, amount_minor=150_000)

        response = create_payment(client, BOOKING, amount_minor=99_000)

        assert response.status_code == 409
        assert response.json()["error"] == "payment_conflict"


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"status": 500}, id="server-error"),
        pytest.param({"status": 201, "delay_ms": int(HTTP_TIMEOUT * 1000) + 500}, id="hangs"),
        pytest.param({"fault": "CONNECTION_RESET_BY_PEER"}, id="connection-reset"),
    ],
)
def test_provider_failure_keeps_the_payment_for_a_retry(
    client: TestClient, stubs: WireMock, failure: dict[str, Any]
) -> None:
    stubs.stub("POST", CHARGES, **failure)
    failed = create_payment(client, BOOKING)

    stubs.reset()
    stub_charge_created(stubs, "ch_retry")
    retried = create_payment(client, BOOKING)

    assert failed.status_code == 503
    assert failed.json()["error"] == "provider_unavailable"
    assert retried.status_code == 200
    assert retried.json()["checkout_url"] == "https://paystub.example/checkout/ch_retry"
    # The retry reuses the key: if the first charge did reach PayStub, it is not repeated.
    [retry] = stubs.received("POST", CHARGES)
    assert retry["headers"]["Idempotency-Key"] == retried.json()["id"]


@pytest.mark.parametrize(
    "body_change",
    [
        pytest.param({"amount_minor": 0}, id="zero-amount"),
        pytest.param({"amount_minor": 1_000_000_001}, id="amount-above-limit"),
        pytest.param({"amount_minor": 2**31}, id="amount-beyond-database-integer"),
        pytest.param({"currency": "rub"}, id="lowercase-currency"),
        pytest.param({"currency": "RUBL"}, id="long-currency"),
        pytest.param({"booking_id": "not-a-uuid"}, id="bad-booking-id"),
    ],
)
def test_malformed_payment_request_is_rejected(
    client: TestClient, stubs: WireMock, body_change: dict[str, Any]
) -> None:
    body = {"booking_id": str(BOOKING), "amount_minor": 150_000, "currency": "RUB"} | body_change

    assert client.post("/payments", json=body).status_code == 422
    assert stubs.received("POST", CHARGES) == []


def test_amount_at_the_limit_is_accepted(client: TestClient, stubs: WireMock) -> None:
    stub_charge_created(stubs)

    assert create_payment(client, BOOKING, amount_minor=1_000_000_000).status_code == 201


def test_unknown_payment_is_not_found(client: TestClient) -> None:
    response = client.get(f"/payments/{uuid.UUID(int=7)}")

    assert response.status_code == 404
    assert response.json()["error"] == "payment_not_found"
