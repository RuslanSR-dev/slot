"""Reads booking events from the broker and turns them into notifications.

The guarantees are spelled out in ADR-0010. In short:

- a message is acknowledged only after the notification is recorded as sent,
  so a crash means a repeat, never a silent loss;
- the repeat is harmless because the event id is the primary key of the
  notification, and the same id goes to the gateway as the idempotency key;
- a message nobody can handle is not retried forever: after
  `max_deliveries` it goes to the dead-letter stream so the rest keep moving.

`run_once` exists for the tests: they drive the consumer step by step instead
of racing a background loop.
"""

import json
import logging
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import redis
from sqlalchemy import create_engine, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from notifier.domain import (
    BookingEvent,
    InvalidEventError,
    NotificationStatus,
    parse_event,
    render,
)
from notifier.gateway import NotifyGateway
from notifier.models import Notification

DEFAULT_STREAM = "slot.bookings.v1"
DEFAULT_GROUP = "notifier"
# The stream of messages we gave up on. Its length is a metric, not a folder
# nobody opens: iteration 5 puts it on the dashboard.
DEAD_SUFFIX = ".dead"
MAX_ERROR_LENGTH = 200

Outcome = Literal["sent", "duplicate", "ignored", "dead", "failed"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConsumerSettings:
    stream: str = DEFAULT_STREAM
    group: str = DEFAULT_GROUP
    consumer: str = "notifier-1"
    batch: int = 10
    block_ms: int = 1000
    # A message taken by a consumer that died is picked up again after this long.
    idle_ms: int = 30_000
    # How many deliveries a message gets before it is declared poisonous.
    max_deliveries: int = 5

    @property
    def dead_stream(self) -> str:
        return f"{self.stream}{DEAD_SUFFIX}"


class Consumer:
    def __init__(
        self,
        broker: redis.Redis,
        session_factory: sessionmaker[Session],
        gateway: NotifyGateway,
        settings: ConsumerSettings | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._broker = broker
        self._sessions = session_factory
        self._gateway = gateway
        self._settings = settings or ConsumerSettings()
        self._clock = clock

    def ensure_group(self) -> None:
        """Create the consumer group. Starting at 0 means nothing published is skipped."""
        try:
            self._broker.xgroup_create(
                self._settings.stream, self._settings.group, id="0", mkstream=True
            )
        except redis.ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    def run_once(self) -> Counter[Outcome]:
        """Handle the messages that are due: abandoned ones first, then new."""
        results: Counter[Outcome] = Counter()
        for message_id, fields, deliveries in self._reclaimed() + self._fresh():
            results[self._handle(message_id, fields, deliveries)] += 1
        return results

    def run_forever(self) -> None:  # pragma: no cover - the loop is the process
        self.ensure_group()
        while True:
            self.run_once()

    def _reclaimed(self) -> list[tuple[str, dict[str, str], int]]:
        """Messages another consumer took and never acknowledged.

        Without this a message held by a process that died would stay in the
        pending list forever, and nobody would notice.
        """
        settings = self._settings
        pending: Any = self._broker.xpending_range(
            settings.stream,
            settings.group,
            min="-",
            max="+",
            count=settings.batch,
            idle=settings.idle_ms,
        )
        if not pending:
            return []
        deliveries = {entry["message_id"]: int(entry["times_delivered"]) for entry in pending}
        claimed: Any = self._broker.xclaim(
            settings.stream,
            settings.group,
            settings.consumer,
            min_idle_time=settings.idle_ms,
            message_ids=list(deliveries),
        )
        # Claiming counts as another delivery, which is what the counter is for.
        return [(message_id, fields, deliveries[message_id] + 1) for message_id, fields in claimed]

    def _fresh(self) -> list[tuple[str, dict[str, str], int]]:
        settings = self._settings
        response: Any = self._broker.xreadgroup(
            settings.group,
            settings.consumer,
            {settings.stream: ">"},
            count=settings.batch,
            # block=0 would wait forever, which is not what "do not wait" means.
            block=settings.block_ms or None,
        )
        return [
            (message_id, fields, 1) for _, entries in response for message_id, fields in entries
        ]

    def _handle(self, message_id: str, fields: dict[str, str], deliveries: int) -> Outcome:
        try:
            event = parse_event(json.loads(fields.get("data", "")))
        except (ValueError, InvalidEventError) as error:
            # A message we cannot read will not become readable on the next try.
            return self._dead_letter(message_id, fields, str(error))

        if deliveries > self._settings.max_deliveries:
            return self._dead_letter(
                message_id, fields, f"delivered {deliveries} times", event.event_id
            )

        text = render(event)
        if text is None:
            # An event type we do not know: acknowledged, not dead-lettered.
            self._broker.xack(self._settings.stream, self._settings.group, message_id)
            return "ignored"

        with self._sessions() as session:
            if self._already_done(session, event, text):
                self._broker.xack(self._settings.stream, self._settings.group, message_id)
                return "duplicate"
            try:
                delivery = self._gateway.send(
                    idempotency_key=event.event_id, recipient=event.client_id, text=text
                )
            except Exception as error:
                # No acknowledgement: the message stays pending and comes back.
                self._record_failure(session, event.event_id, str(error))
                logger.warning("notification for %s failed: %s", event.event_id, error)
                return "failed"
            self._mark_sent(session, event.event_id, delivery.id)
        self._broker.xack(self._settings.stream, self._settings.group, message_id)
        return "sent"

    def _already_done(self, session: Session, event: BookingEvent, text: str) -> bool:
        """Claim the event, or report that it has already been dealt with.

        The primary key decides, so two consumers holding the same event at the
        same time cannot both send a message (ADR-0010).
        """
        now = self._clock()
        claimed = session.scalar(
            insert(Notification)
            .values(
                event_id=event.event_id,
                booking_id=event.booking_id,
                client_id=event.client_id,
                event_type=event.type,
                text=text,
                status=NotificationStatus.SENDING,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[Notification.event_id])
            .returning(Notification.event_id)
        )
        session.commit()
        if claimed is not None:
            return False
        existing = session.get(Notification, event.event_id)
        # `sending` means a previous attempt died between claiming and sending.
        # Trying again is safe: the gateway gets the same idempotency key.
        return existing is not None and existing.status != NotificationStatus.SENDING

    def _record_failure(self, session: Session, event_id: str, error: str) -> None:
        session.execute(
            update(Notification)
            .where(Notification.event_id == event_id)
            .values(
                attempts=Notification.attempts + 1,
                last_error=error[:MAX_ERROR_LENGTH],
                updated_at=self._clock(),
            )
        )
        session.commit()

    def _mark_sent(self, session: Session, event_id: str, gateway_message_id: str) -> None:
        session.execute(
            update(Notification)
            .where(Notification.event_id == event_id)
            .values(
                status=NotificationStatus.SENT,
                gateway_message_id=gateway_message_id,
                updated_at=self._clock(),
            )
        )
        session.commit()

    def _dead_letter(
        self, message_id: str, fields: dict[str, str], reason: str, event_id: str | None = None
    ) -> Outcome:
        # The dead-letter entry says what arrived and why it was given up on:
        # whoever looks into this stream has no other source of that.
        self._broker.xadd(
            self._settings.dead_stream,
            {
                "original_id": message_id,
                "reason": reason[:MAX_ERROR_LENGTH],
                "topic": fields.get("topic", ""),
                "data": fields.get("data", ""),
            },
        )
        if event_id is not None:
            with self._sessions() as session:
                session.execute(
                    update(Notification)
                    .where(Notification.event_id == event_id)
                    .values(
                        status=NotificationStatus.DEAD,
                        last_error=reason[:MAX_ERROR_LENGTH],
                        updated_at=self._clock(),
                    )
                )
                session.commit()
        # Acknowledged on purpose: one poisonous message must not stop the rest.
        self._broker.xack(self._settings.stream, self._settings.group, message_id)
        logger.warning("dead-lettered %s: %s", message_id, reason)
        return "dead"


def main() -> None:  # pragma: no cover - entry point of the worker container
    logging.basicConfig(level=logging.INFO)
    engine = create_engine(os.environ["SLOT_DATABASE_URL"], pool_size=2, pool_pre_ping=True)
    broker = redis.Redis.from_url(os.environ["SLOT_REDIS_URL"], decode_responses=True)
    gateway = NotifyGateway(os.environ["SLOT_NOTIFYGW_URL"], os.environ["SLOT_NOTIFYGW_API_KEY"])
    settings = ConsumerSettings(
        stream=os.environ.get("SLOT_EVENTS_STREAM", DEFAULT_STREAM),
        consumer=os.environ.get("HOSTNAME", "notifier-1"),
    )
    Consumer(broker, sessionmaker(engine), gateway, settings).run_forever()


if __name__ == "__main__":
    main()
