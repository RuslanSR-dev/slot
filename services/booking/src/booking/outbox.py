"""Publishing of outbox messages: the only place in booking that knows the broker.

The relay runs as its own process (`python -m booking.outbox`). A message is
marked published only after the broker accepted it, so a crash in between
means a repeat, never a loss (ADR-0010).
"""

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import redis
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from booking.models import OutboxMessage

DEFAULT_STREAM = "slot.bookings.v1"
# The stream is a log, not storage: old entries are trimmed (ADR-0009).
STREAM_MAXLEN = 10_000
BATCH_SIZE = 100
POLL_SECONDS = 0.5
MAX_ERROR_LENGTH = 200

logger = logging.getLogger(__name__)


class Publisher(Protocol):
    def publish(self, topic: str, payload: dict[str, object]) -> None: ...


class RedisPublisher:
    def __init__(self, url: str, stream: str = DEFAULT_STREAM, timeout: float = 5.0) -> None:
        self._redis = redis.Redis.from_url(
            url, socket_timeout=timeout, socket_connect_timeout=timeout
        )
        self._stream = stream

    def publish(self, topic: str, payload: dict[str, object]) -> None:
        # One field with the whole message: the envelope is JSON, the same
        # bytes the committed schema describes (contracts/events).
        self._redis.xadd(
            self._stream,
            {"topic": topic, "data": json.dumps(payload)},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )

    def close(self) -> None:
        self._redis.close()


def publish_pending(
    session: Session, publisher: Publisher, now: datetime, batch_size: int = BATCH_SIZE
) -> int:
    """Publish unpublished messages in creation order. Returns how many were sent.

    The row is locked while it is being published: that is what keeps two
    relays from publishing the same message twice. `SKIP LOCKED` lets the
    other relay work on the rest of the batch instead of waiting.
    """
    messages = session.scalars(
        select(OutboxMessage)
        .where(OutboxMessage.published_at.is_(None))
        .order_by(OutboxMessage.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()

    published = 0
    for message in messages:
        try:
            publisher.publish(message.topic, message.payload)
        except Exception as error:
            # The broker is down. Keep the message, count the attempt and stop:
            # the next messages would fail the same way.
            session.rollback()
            _record_failure(session, message.id, str(error))
            logger.warning("outbox publish failed: %s", error)
            break
        message.published_at = now
        published += 1
    session.commit()
    return published


def touch_heartbeat(path: str | None, now: datetime) -> None:
    """Say that the loop is still turning.

    A process without an HTTP port has nothing to answer a healthcheck with.
    Writing the time of every cycle into a file gives the container something
    to check that is not "the process exists": a loop stuck on a call nobody
    times out stops updating it.
    """
    if path is None:
        return
    Path(path).write_text(now.isoformat())


def _record_failure(session: Session, message_id: object, error: str) -> None:
    session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.id == message_id)
        .values(attempts=OutboxMessage.attempts + 1, last_error=error[:MAX_ERROR_LENGTH])
    )
    session.commit()


def run_forever(
    session_factory: sessionmaker[Session],
    publisher: Publisher,
    poll: float = POLL_SECONDS,
    heartbeat: str | None = None,
) -> None:  # pragma: no cover - the loop is the process, its body is tested
    while True:
        now = datetime.now(UTC)
        touch_heartbeat(heartbeat, now)
        with session_factory() as session:
            sent = publish_pending(session, publisher, now)
        if sent == 0:
            time.sleep(poll)


def main() -> None:  # pragma: no cover - entry point of the relay container
    logging.basicConfig(level=logging.INFO)
    engine = create_engine(os.environ["SLOT_DATABASE_URL"], pool_size=2, pool_pre_ping=True)
    publisher = RedisPublisher(
        os.environ["SLOT_REDIS_URL"], os.environ.get("SLOT_EVENTS_STREAM", DEFAULT_STREAM)
    )
    run_forever(sessionmaker(engine), publisher, heartbeat=os.environ.get("SLOT_HEARTBEAT_FILE"))


if __name__ == "__main__":
    main()
