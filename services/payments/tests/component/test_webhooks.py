"""PayStub webhooks: signed, delivered at least once, sometimes out of order.

Since ADR-0010 the webhook does not call booking itself. It records the
event and writes a command into the outbox; the relay delivers it. So the
tests here run the relay wherever the old ones expected a neighbour to have
been called already.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from .conftest import (
    CONFIRM,
    create_payment,
    outbox,
    paystub_event,
    send_webhook,
    stub_booking_confirm,
    stub_charge_created,
)
from .wiremock import WireMock


@pytest.fixture
def payment(client: TestClient, stubs: WireMock) -> Any:
    stub_charge_created(stubs)
    return create_payment(client, uuid.UUID(int=42)).json()


def confirmations(stubs: WireMock) -> int:
    return len(stubs.received("POST", CONFIRM, pattern=True))


def status_of(client: TestClient, payment: Any) -> Any:
    return client.get(f"/payments/{payment['id']}").json()


class TestSuccessfulCharge:
    def test_payment_succeeds_and_the_confirmation_is_handed_to_the_relay(
        self,
        client: TestClient,
        stubs: WireMock,
        payment: Any,
        engine: Engine,
        relay: Callable[[], int],
    ) -> None:
        stub_booking_confirm(stubs)

        response = send_webhook(client, paystub_event(payment["id"]))

        assert response.status_code == 200
        assert response.json() == {"outcome": "processed"}
        assert status_of(client, payment)["status"] == "succeeded"
        # The answer to PayStub did not wait for booking: the command is here.
        [command] = outbox(engine)
        assert command["topic"] == "booking.confirm"
        assert confirmations(stubs) == 0

        assert relay() == 1

        [confirm] = stubs.received("POST", CONFIRM, pattern=True)
        assert confirm["url"] == f"/internal/bookings/{payment['booking_id']}/confirm"
        assert outbox(engine)[0]["published_at"] is not None

    def test_failed_charge_does_not_confirm_the_booking(
        self,
        client: TestClient,
        stubs: WireMock,
        payment: Any,
        engine: Engine,
        relay: Callable[[], int],
    ) -> None:
        response = send_webhook(client, paystub_event(payment["id"], "charge.failed"))

        assert response.json() == {"outcome": "processed"}
        assert status_of(client, payment)["status"] == "failed"
        assert outbox(engine) == []
        assert relay() == 0
        assert confirmations(stubs) == 0


class TestDeliveredMoreThanOnce:
    def test_same_event_twice_confirms_the_booking_once(
        self, client: TestClient, stubs: WireMock, payment: Any, relay: Callable[[], int]
    ) -> None:
        stub_booking_confirm(stubs)
        event = paystub_event(payment["id"])

        first = send_webhook(client, event)
        second = send_webhook(client, event)
        relay()

        assert first.json() == {"outcome": "processed"}
        assert second.status_code == 200, "a repeat must be acknowledged, or PayStub keeps retrying"
        assert second.json() == {"outcome": "duplicate"}
        assert confirmations(stubs) == 1

    def test_second_success_event_for_a_paid_payment_changes_nothing(
        self, client: TestClient, stubs: WireMock, payment: Any, relay: Callable[[], int]
    ) -> None:
        stub_booking_confirm(stubs)
        send_webhook(client, paystub_event(payment["id"]))

        response = send_webhook(client, paystub_event(payment["id"]))
        relay()

        assert response.json() == {"outcome": "ignored"}
        assert confirmations(stubs) == 1

    def test_failure_arriving_after_success_does_not_undo_it(
        self, client: TestClient, stubs: WireMock, payment: Any
    ) -> None:
        stub_booking_confirm(stubs)
        send_webhook(client, paystub_event(payment["id"]))

        response = send_webhook(client, paystub_event(payment["id"], "charge.failed"))

        assert response.json() == {"outcome": "ignored"}
        assert status_of(client, payment)["status"] == "succeeded"


class TestBookingSide:
    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param({"status": 500}, id="server-error"),
            pytest.param({"fault": "CONNECTION_RESET_BY_PEER"}, id="connection-reset"),
        ],
    )
    def test_booking_unavailable_no_longer_makes_the_provider_retry(
        self,
        client: TestClient,
        stubs: WireMock,
        payment: Any,
        engine: Engine,
        relay: Callable[[], int],
        failure: dict[str, Any],
    ) -> None:
        """This is what ADR-0010 changed, and the change is deliberate.

        Before, the confirmation happened inside the webhook's transaction: a
        neighbour that was down meant `503` and another delivery from PayStub.
        Now the event is safely recorded and the command waits in the outbox,
        so PayStub is done and the retrying is our own business.
        """
        stub_booking_confirm(stubs, **failure)

        accepted = send_webhook(client, paystub_event(payment["id"]))

        assert accepted.status_code == 200
        assert accepted.json() == {"outcome": "processed"}
        assert relay() == 0
        [command] = outbox(engine)
        assert command["published_at"] is None
        assert command["attempts"] == 1

        stubs.reset()
        stub_booking_confirm(stubs)

        assert relay() == 1
        assert confirmations(stubs) == 1
        assert status_of(client, payment)["status"] == "succeeded"


class TestUntrustedInput:
    @pytest.mark.parametrize(
        "signature",
        [
            pytest.param(None, id="missing"),
            pytest.param("sha256=" + "0" * 64, id="forged"),
        ],
    )
    def test_unsigned_webhook_is_rejected_and_not_recorded(
        self, client: TestClient, stubs: WireMock, payment: Any, signature: str | None
    ) -> None:
        stub_booking_confirm(stubs)
        event = paystub_event(payment["id"])

        rejected = send_webhook(client, event, signature=signature)
        genuine = send_webhook(client, event)

        assert rejected.status_code == 401
        assert rejected.json()["error"] == "invalid_signature"
        assert genuine.json() == {"outcome": "processed"}, "a forged copy must not block it"

    @pytest.mark.parametrize(
        "event",
        [
            pytest.param({"id": "evt_1", "type": "charge.succeeded"}, id="no-data"),
            pytest.param(
                {
                    "id": "evt_\x00",
                    "type": "charge.succeeded",
                    "data": {"charge_id": "c", "reference": "r"},
                },
                id="nul-byte-in-event-id",
            ),
        ],
    )
    def test_signed_but_malformed_event_is_rejected(
        self, client: TestClient, event: dict[str, Any]
    ) -> None:
        response = send_webhook(client, event)

        assert response.status_code == 422
        assert response.json()["error"] == "invalid_event"

    @pytest.mark.parametrize(
        "event_change",
        [
            pytest.param({"type": "customer.created"}, id="event-we-do-not-handle"),
            pytest.param(
                {"data": {"charge_id": "ch_1", "reference": str(uuid.UUID(int=9))}},
                id="unknown-payment",
            ),
            pytest.param(
                {"data": {"charge_id": "ch_1", "reference": "not-a-uuid"}}, id="garbage-reference"
            ),
        ],
    )
    def test_event_a_retry_cannot_fix_is_acknowledged(
        self, client: TestClient, stubs: WireMock, payment: Any, event_change: dict[str, Any]
    ) -> None:
        response = send_webhook(client, paystub_event(payment["id"]) | event_change)

        assert response.status_code == 200
        assert response.json() == {"outcome": "ignored"}
        assert confirmations(stubs) == 0
