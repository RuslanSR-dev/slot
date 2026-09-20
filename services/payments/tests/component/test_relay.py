"""The outbox relay of payments: confirmations and refunds (ADR-0010).

The interesting case is the one nobody wants to think about - the money
arrived for a booking that no longer exists. It has to end with the client
getting the money back, and it has to survive a PayStub that is down in the
middle of it.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from .conftest import (
    REFUNDS,
    create_payment,
    outbox,
    paystub_event,
    send_webhook,
    stub_booking_confirm,
    stub_charge_created,
    stub_refund_created,
)
from .wiremock import WireMock


@pytest.fixture
def paid_payment(client: TestClient, stubs: WireMock) -> Any:
    """A payment whose money already arrived: the webhook has been handled."""
    stub_charge_created(stubs)
    payment = create_payment(client, uuid.UUID(int=77)).json()
    send_webhook(client, paystub_event(payment["id"]))
    return payment


def refunds(stubs: WireMock) -> list[dict[str, Any]]:
    return stubs.received("POST", REFUNDS)


def payment_state(client: TestClient, payment: Any) -> Any:
    return client.get(f"/payments/{payment['id']}").json()


class TestRefundOfABookingThatIsGone:
    def test_a_booking_that_cannot_be_confirmed_ends_with_a_refund(
        self,
        client: TestClient,
        stubs: WireMock,
        paid_payment: Any,
        engine: Engine,
        relay: Callable[[], int],
    ) -> None:
        stub_booking_confirm(stubs, status=409, json_body={"error": "invalid_transition"})
        stub_refund_created(stubs)

        # First the confirmation is refused, which writes the refund command.
        assert relay() == 1
        assert payment_state(client, paid_payment)["needs_refund"] is True
        assert [command["topic"] for command in outbox(engine)] == [
            "booking.confirm",
            "payment.refund",
        ]

        assert relay() == 1

        [refund] = refunds(stubs)
        assert refund["headers"]["Idempotency-Key"] == paid_payment["id"]
        after = payment_state(client, paid_payment)
        assert after["needs_refund"] is False
        assert all(command["published_at"] is not None for command in outbox(engine))

    def test_a_provider_that_is_down_does_not_lose_the_refund(
        self,
        client: TestClient,
        stubs: WireMock,
        paid_payment: Any,
        engine: Engine,
        relay: Callable[[], int],
    ) -> None:
        """Money owed to a client must not depend on PayStub being up right now."""
        stub_booking_confirm(stubs, status=409, json_body={"error": "invalid_transition"})
        stubs.stub("POST", REFUNDS, status=500)
        relay()

        assert relay() == 0

        refund_command = outbox(engine)[1]
        assert refund_command["published_at"] is None
        assert refund_command["attempts"] == 1
        assert payment_state(client, paid_payment)["needs_refund"] is True

        stubs.reset()
        stub_refund_created(stubs)

        assert relay() == 1
        assert len(refunds(stubs)) == 1
        assert payment_state(client, paid_payment)["needs_refund"] is False

    def test_a_refund_command_delivered_twice_refunds_once(
        self,
        client: TestClient,
        stubs: WireMock,
        paid_payment: Any,
        engine: Engine,
        relay: Callable[[], int],
        session_factory: Any,
    ) -> None:
        """Two relays, or one relay after a crash: the money leaves once."""
        from payments.domain import REFUND_PAYMENT
        from payments.models import OutboxMessage

        stub_booking_confirm(stubs, status=409, json_body={"error": "invalid_transition"})
        stub_refund_created(stubs)
        relay()
        relay()

        # The same command again, as a crashed relay would leave behind.
        with session_factory() as session:
            session.add(
                OutboxMessage(topic=REFUND_PAYMENT, payload={"payment_id": paid_payment["id"]})
            )
            session.commit()

        assert relay() == 1

        assert len(refunds(stubs)) == 1, "the refund is already done; PayStub is not asked again"


class TestUnknownCommands:
    def test_a_command_the_relay_does_not_know_is_not_silently_dropped(
        self, session_factory: Any, engine: Engine, relay: Callable[[], int]
    ) -> None:
        """A typo in a topic must be loud: the command stays and the attempts grow."""
        from payments.models import OutboxMessage

        with session_factory() as session:
            session.add(OutboxMessage(topic="booking.confrim", payload={}))
            session.commit()

        assert relay() == 0

        [command] = outbox(engine)
        assert command["published_at"] is None
        assert "unknown outbox command" in command["last_error"]
