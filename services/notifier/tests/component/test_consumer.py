"""What happens to a message on its way to the client (ADR-0010).

Every test here puts the failure in a different place: before sending, after
sending, in the message itself. A consumer that is only tested on the happy
path proves nothing - the whole reason it exists is that things fail.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
import redis
from sqlalchemy import Engine

from notifier.consumer import Consumer, ConsumerSettings
from notifier.domain import NotificationStatus

from .conftest import MESSAGES, notifications, stub_gateway_accepts
from .wiremock import WireMock


class DyingGateway:
    """The process dies mid-flight: no error handling of ours runs at all.

    `KeyboardInterrupt` is not an `Exception`, so it goes straight through the
    consumer - which is exactly what a killed process looks like from the
    outside: no rollback bookkeeping, no acknowledgement, nothing written.
    """

    def __init__(self, real: Any = None) -> None:
        self._real = real

    def send(self, idempotency_key: str, recipient: str, text: str) -> Any:
        if self._real is not None:
            self._real.send(idempotency_key=idempotency_key, recipient=recipient, text=text)
        raise KeyboardInterrupt("process killed")


def sent_requests(notifygw: WireMock) -> list[dict[str, Any]]:
    return notifygw.received("POST", MESSAGES)


def idempotency_keys(notifygw: WireMock) -> list[str]:
    return [request["headers"]["Idempotency-Key"] for request in sent_requests(notifygw)]


def pending_count(broker: redis.Redis, settings: ConsumerSettings) -> int:
    summary: Any = broker.xpending(settings.stream, settings.group)
    return int(summary["pending"])


class TestHappyPath:
    def test_an_event_becomes_one_message_to_the_client(
        self,
        consumer: Consumer,
        publish: Callable[..., str],
        notifygw: WireMock,
        engine: Engine,
        consumer_settings: ConsumerSettings,
        broker: redis.Redis,
    ) -> None:
        stub_gateway_accepts(notifygw)
        event_id = publish(client_id="client-7")

        assert consumer.run_once()["sent"] == 1

        [request] = sent_requests(notifygw)
        assert request["headers"]["Idempotency-Key"] == event_id
        assert json.loads(request["body"]) == {
            "recipient": "client-7",
            "text": "Your booking on 2026-09-21 10:30 is confirmed.",
        }
        [notification] = notifications(engine)
        assert notification["status"] == NotificationStatus.SENT
        assert notification["gateway_message_id"] == "msg_1"
        # Acknowledged, so no other consumer will ever pick it up again.
        assert pending_count(broker, consumer_settings) == 0

    def test_events_of_one_booking_are_handled_in_any_order(
        self, consumer: Consumer, publish: Callable[..., str], notifygw: WireMock, engine: Engine
    ) -> None:
        """A consumer group gives no order guarantee, so we must not need one."""
        stub_gateway_accepts(notifygw)
        booking_id = "booking-1"
        publish(type="booking.cancelled", booking_id=booking_id)
        publish(type="booking.confirmed", booking_id=booking_id)

        consumer.run_once()

        assert [row["event_type"] for row in notifications(engine)] == [
            "booking.cancelled",
            "booking.confirmed",
        ]
        assert len(sent_requests(notifygw)) == 2

    def test_an_event_type_we_do_not_know_is_acknowledged_and_skipped(
        self,
        consumer: Consumer,
        publish: Callable[..., str],
        notifygw: WireMock,
        engine: Engine,
        broker: redis.Redis,
        consumer_settings: ConsumerSettings,
    ) -> None:
        """The producer may learn a new event tomorrow; that must not stop us."""
        stub_gateway_accepts(notifygw)
        publish(type="booking.rescheduled")

        assert consumer.run_once()["ignored"] == 1

        assert sent_requests(notifygw) == []
        assert notifications(engine) == []
        assert pending_count(broker, consumer_settings) == 0


class TestDuplicates:
    def test_the_same_event_delivered_twice_is_one_message(
        self,
        consumer: Consumer,
        publish: Callable[..., str],
        notifygw: WireMock,
        engine: Engine,
        broker: redis.Redis,
        consumer_settings: ConsumerSettings,
    ) -> None:
        """At-least-once delivery means this happens, not that it might."""
        stub_gateway_accepts(notifygw)
        event_id = publish()
        # The same event, published twice: two stream entries, one event id.
        publish(event_id=event_id)

        results = consumer.run_once()

        assert (results["sent"], results["duplicate"]) == (1, 1)
        assert len(sent_requests(notifygw)) == 1
        assert len(notifications(engine)) == 1
        assert pending_count(broker, consumer_settings) == 0


class TestGatewayFailures:
    def test_a_gateway_that_is_down_keeps_the_message(
        self,
        consumer: Consumer,
        publish: Callable[..., str],
        notifygw: WireMock,
        engine: Engine,
        broker: redis.Redis,
        consumer_settings: ConsumerSettings,
    ) -> None:
        notifygw.stub("POST", MESSAGES, status=503)
        event_id = publish()

        assert consumer.run_once()["failed"] == 1

        [notification] = notifications(engine)
        assert notification["status"] == NotificationStatus.SENDING
        assert notification["attempts"] == 1
        assert "503" in notification["last_error"]
        # Not acknowledged: the message is still ours to finish.
        assert pending_count(broker, consumer_settings) == 1

        notifygw.reset()
        stub_gateway_accepts(notifygw)
        assert consumer.run_once()["sent"] == 1
        assert idempotency_keys(notifygw) == [event_id]
        assert notifications(engine)[0]["status"] == NotificationStatus.SENT

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param({"delay_ms": 1500}, id="hangs"),
            pytest.param({"fault": "CONNECTION_RESET_BY_PEER"}, id="drops-the-connection"),
        ],
    )
    def test_a_gateway_that_never_answers_is_a_retry_not_a_lost_message(
        self,
        consumer: Consumer,
        publish: Callable[..., str],
        notifygw: WireMock,
        engine: Engine,
        broker: redis.Redis,
        consumer_settings: ConsumerSettings,
        failure: dict[str, Any],
    ) -> None:
        """A neighbour that hangs looks different from one that answers 503."""
        notifygw.stub("POST", MESSAGES, **failure)
        publish()

        assert consumer.run_once()["failed"] == 1

        assert notifications(engine)[0]["status"] == NotificationStatus.SENDING
        assert pending_count(broker, consumer_settings) == 1

    def test_a_message_nobody_can_deliver_stops_after_a_known_number_of_tries(
        self,
        consumer: Consumer,
        publish: Callable[..., str],
        notifygw: WireMock,
        engine: Engine,
        broker: redis.Redis,
        consumer_settings: ConsumerSettings,
    ) -> None:
        """Otherwise one message would be retried forever, at the gateway's expense."""
        notifygw.stub("POST", MESSAGES, status=422)
        publish()

        outcomes = [consumer.run_once() for _ in range(consumer_settings.max_deliveries + 1)]

        assert [result.most_common(1)[0][0] for result in outcomes] == ["failed"] * 3 + ["dead"]
        assert notifications(engine)[0]["status"] == NotificationStatus.DEAD
        assert broker.xlen(consumer_settings.dead_stream) == 1
        # Acknowledged after the dead-letter: it no longer holds the group back.
        assert pending_count(broker, consumer_settings) == 0


class TestPoisonMessages:
    def test_a_message_we_cannot_read_goes_to_the_dead_letter_stream(
        self,
        consumer: Consumer,
        publish: Callable[..., str],
        notifygw: WireMock,
        broker: redis.Redis,
        engine: Engine,
        consumer_settings: ConsumerSettings,
    ) -> None:
        """And it must not take the rest of the stream down with it."""
        stub_gateway_accepts(notifygw)
        broker.xadd(consumer_settings.stream, {"topic": "booking.confirmed", "data": "{not json"})
        publish(client_id="client-9")

        results = consumer.run_once()

        assert (results["dead"], results["sent"]) == (1, 1)
        assert len(notifications(engine)) == 1
        dead_entries: Any = broker.xrange(consumer_settings.dead_stream)
        [(_, dead)] = dead_entries
        assert "{not json" in dead["data"]
        assert pending_count(broker, consumer_settings) == 0

    def test_an_event_without_the_fields_we_need_is_not_retried(
        self,
        consumer: Consumer,
        publish: Callable[..., str],
        notifygw: WireMock,
        broker: redis.Redis,
        consumer_settings: ConsumerSettings,
    ) -> None:
        stub_gateway_accepts(notifygw)
        publish(client_id="")

        assert consumer.run_once()["dead"] == 1

        assert sent_requests(notifygw) == []
        assert broker.xlen(consumer_settings.dead_stream) == 1


class TestCrashes:
    """A killed process is not a caught exception: nothing of ours runs afterwards."""

    def test_a_crash_before_sending_delivers_the_message_once(
        self,
        make_consumer: Callable[..., Consumer],
        publish: Callable[..., str],
        notifygw: WireMock,
        engine: Engine,
    ) -> None:
        stub_gateway_accepts(notifygw)
        event_id = publish()
        dying = make_consumer(DyingGateway())

        with pytest.raises(KeyboardInterrupt):
            dying.run_once()

        assert sent_requests(notifygw) == []
        assert notifications(engine)[0]["status"] == NotificationStatus.SENDING

        # The process comes back. The message was never acknowledged, so it is
        # still there, and this time it goes through.
        assert make_consumer(name="notifier-2").run_once()["sent"] == 1
        assert idempotency_keys(notifygw) == [event_id]

    def test_a_crash_after_sending_does_not_reach_the_client_twice(
        self,
        make_consumer: Callable[..., Consumer],
        publish: Callable[..., str],
        notifygw: WireMock,
        gateway: Any,
        engine: Engine,
    ) -> None:
        """There is no exactly-once delivery, only an exactly-once effect.

        The message does leave twice - we cannot prevent that. It carries the
        same idempotency key both times, so NotifyGate turns the second one
        into the first one instead of writing to the client again (ADR-0010).
        """
        stub_gateway_accepts(notifygw)
        event_id = publish()
        dying = make_consumer(DyingGateway(gateway))

        with pytest.raises(KeyboardInterrupt):
            dying.run_once()

        assert idempotency_keys(notifygw) == [event_id]

        assert make_consumer(name="notifier-2").run_once()["sent"] == 1

        assert idempotency_keys(notifygw) == [event_id, event_id]
        [notification] = notifications(engine)
        assert notification["status"] == NotificationStatus.SENT
