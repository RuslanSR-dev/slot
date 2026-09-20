"""Events leave the service through the outbox (ADR-0010).

The point of these tests is the moment of failure. A booking that is
confirmed without an event, or an event published without the booking,
is a bug that only shows up when something dies in between - so the tests
put the failure exactly there.
"""

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from booking.outbox import DEFAULT_STREAM, RedisPublisher, publish_pending

from .conftest import NOW, PENDING_TTL, FixedClock
from .test_bookings_api import HOUR, book, create_slot


def outbox(engine: Engine) -> list[dict[str, Any]]:
    """Outbox rows in creation order: uuid7 ids sort by time."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, topic, payload, published_at, attempts, last_error"
                " FROM outbox_messages ORDER BY id"
            )
        ).mappings()
        return [dict(row) for row in rows]


def stream_events(broker: redis.Redis, stream: str = DEFAULT_STREAM) -> list[dict[str, Any]]:
    entries: Any = broker.xrange(stream)
    return [json.loads(fields["data"]) for _, fields in entries]


def confirm(client: TestClient, booking_id: str) -> Any:
    return client.post(f"/internal/bookings/{booking_id}/confirm")


class BrokenPublisher:
    """A broker that is down. The message must survive it."""

    def publish(self, topic: str, payload: dict[str, object]) -> None:
        raise ConnectionError("connection refused")


class TestOutboxIsWrittenWithTheChange:
    def test_confirmation_writes_one_event(self, client: TestClient, engine: Engine) -> None:
        slot = create_slot(client)
        booking = book(client, slot["id"]).json()

        confirm(client, booking["id"])

        [message] = outbox(engine)
        assert message["topic"] == "booking.confirmed"
        assert message["published_at"] is None
        assert message["payload"] == {
            "event_id": str(message["id"]),
            "type": "booking.confirmed",
            "version": 1,
            "occurred_at": NOW.isoformat(),
            "booking_id": booking["id"],
            "slot_id": slot["id"],
            "client_id": booking["client_id"],
            # Our own rendering, not the one the API happens to use for JSON.
            "starts_at": (NOW + HOUR).isoformat(),
        }

    def test_repeated_confirmation_does_not_produce_a_second_event(
        self, client: TestClient, engine: Engine
    ) -> None:
        """A retry of a lost response must not send the client two notifications."""
        booking = book(client, create_slot(client)["id"]).json()

        assert confirm(client, booking["id"]).status_code == 200
        assert confirm(client, booking["id"]).status_code == 200

        assert [message["topic"] for message in outbox(engine)] == ["booking.confirmed"]

    def test_cancellation_writes_an_event(self, client: TestClient, engine: Engine) -> None:
        booking = book(client, create_slot(client)["id"]).json()

        client.delete(f"/bookings/{booking['id']}")

        assert [message["topic"] for message in outbox(engine)] == ["booking.cancelled"]

    def test_rejected_transition_writes_nothing(self, client: TestClient, engine: Engine) -> None:
        """No event without the change it describes: a cancelled booking cannot be confirmed."""
        booking = book(client, create_slot(client)["id"]).json()
        client.delete(f"/bookings/{booking['id']}")

        assert confirm(client, booking["id"]).status_code == 409

        assert [message["topic"] for message in outbox(engine)] == ["booking.cancelled"]

    def test_expiry_writes_an_event(
        self, client: TestClient, engine: Engine, clock: FixedClock
    ) -> None:
        """The booking expires when someone else wants the slot; the event says so."""
        slot = create_slot(client, hours_from_now=5)
        abandoned = book(client, slot["id"], client_id="client-1").json()

        clock.now = NOW + PENDING_TTL + timedelta(seconds=1)
        assert book(client, slot["id"], client_id="client-2").status_code == 201

        [message] = outbox(engine)
        assert message["topic"] == "booking.expired"
        assert message["payload"]["booking_id"] == abandoned["id"]
        assert message["payload"]["client_id"] == "client-1"


class TestRelay:
    def test_messages_are_published_once_and_in_order(
        self,
        client: TestClient,
        engine: Engine,
        session_factory: sessionmaker[Session],
        broker: redis.Redis,
        redis_url: str,
    ) -> None:
        first = book(client, create_slot(client, hours_from_now=1)["id"]).json()
        second = book(client, create_slot(client, hours_from_now=2)["id"]).json()
        confirm(client, first["id"])
        client.delete(f"/bookings/{second['id']}")
        publisher = RedisPublisher(redis_url)

        with session_factory() as session:
            assert publish_pending(session, publisher, NOW) == 2
            # A second run has nothing left to do: published rows are not sent again.
            assert publish_pending(session, publisher, NOW) == 0

        assert [event["type"] for event in stream_events(broker)] == [
            "booking.confirmed",
            "booking.cancelled",
        ]
        assert all(message["published_at"] is not None for message in outbox(engine))
        publisher.close()

    def test_a_broker_that_is_down_does_not_lose_the_event(
        self,
        client: TestClient,
        engine: Engine,
        session_factory: sessionmaker[Session],
        broker: redis.Redis,
        redis_url: str,
    ) -> None:
        """The proof the outbox exists for: confirming must not depend on the broker."""
        booking = book(client, create_slot(client)["id"]).json()

        assert confirm(client, booking["id"]).status_code == 200
        assert client.get(f"/bookings/{booking['id']}").json()["status"] == "confirmed"

        with session_factory() as session:
            assert publish_pending(session, BrokenPublisher(), NOW) == 0

        [message] = outbox(engine)
        assert message["published_at"] is None
        assert message["attempts"] == 1
        assert "connection refused" in message["last_error"]

        # The broker is back: nothing was lost while it was gone.
        publisher = RedisPublisher(redis_url)
        with session_factory() as session:
            assert publish_pending(session, publisher, NOW) == 1
        assert [event["booking_id"] for event in stream_events(broker)] == [booking["id"]]
        publisher.close()

    def test_a_broken_message_stops_the_batch_instead_of_skipping_it(
        self, client: TestClient, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        """Order matters more than throughput: we never step over an unpublished message."""
        for hours in (1, 2):
            confirm(
                client, book(client, create_slot(client, hours_from_now=hours)["id"]).json()["id"]
            )

        with session_factory() as session:
            assert publish_pending(session, BrokenPublisher(), NOW) == 0

        attempts = [message["attempts"] for message in outbox(engine)]
        assert attempts == [1, 0], "the second message must not even be attempted"


@pytest.mark.parametrize("topic", ["booking.confirmed"])
def test_publisher_writes_into_the_versioned_stream(
    broker: redis.Redis, redis_url: str, topic: str
) -> None:
    """The stream name carries the schema version (ADR-0009)."""
    publisher = RedisPublisher(redis_url)

    publisher.publish(topic, {"event_id": str(uuid.uuid7()), "type": topic})

    assert broker.exists(DEFAULT_STREAM) == 1
    assert broker.xlen(DEFAULT_STREAM) == 1
    publisher.close()
