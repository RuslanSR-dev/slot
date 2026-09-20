"""Component level: the notifier with a real Postgres and a real broker.

The gateway is a WireMock stub reached over real HTTP, so timeouts, 5xx and
what we actually sent are all visible (ADR-0006).
"""

import json
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer

from notifier import migrate
from notifier.app import Settings, create_app
from notifier.consumer import Consumer, ConsumerSettings
from notifier.gateway import NotifyGateway

from .wiremock import WIREMOCK_IMAGE, WireMock

# Same images as compose.yaml: tests must run against what we ship with.
POSTGRES_IMAGE = "postgres:18-alpine"
REDIS_IMAGE = "redis:8.2-alpine"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
STARTS_AT = datetime(2026, 9, 21, 10, 30, tzinfo=UTC)
MESSAGES = "/v1/messages"
GATEWAY_API_KEY = "test-api-key"
# Short, so tests of a hanging gateway take a fraction of a second.
HTTP_TIMEOUT = 0.5
# In tests nothing waits: an unacknowledged message may be reclaimed at once,
# and reading new messages must not block.
TEST_IDLE_MS = 0
MAX_DELIVERIES = 3


def wait_until(condition: Callable[[], bool], timeout: float, what: str) -> None:
    """Poll a condition instead of sleeping a fixed time."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} did not happen within {timeout}s")
        time.sleep(0.01)


@dataclass
class FixedClock:
    """A clock the test controls: time moves only when the test says so."""

    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        yield container


@pytest.fixture(scope="session")
def database_url(postgres: PostgresContainer) -> str:
    url = postgres.get_connection_url()
    migrate.upgrade(url)
    return url


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    engine = create_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(request: pytest.FixtureRequest) -> Iterator[None]:
    # Only tests that touch the shared database need cleaning.
    uses_database = "database_url" in request.fixturenames
    engine: Engine | None = request.getfixturevalue("engine") if uses_database else None
    yield
    if engine is not None:
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE notifications"))


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine)


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer(REDIS_IMAGE) as container:
        yield container


@pytest.fixture
def broker(redis_container: RedisContainer) -> Iterator[redis.Redis]:
    """A real broker, empty at the start of every test."""
    host = redis_container.get_container_host_ip()
    client = redis.Redis.from_url(
        f"redis://{host}:{redis_container.get_exposed_port(6379)}/0", decode_responses=True
    )
    client.flushall()
    yield client
    client.close()


@pytest.fixture(scope="session")
def wiremock_server() -> Iterator[WireMock]:
    with DockerContainer(WIREMOCK_IMAGE).with_exposed_ports(8080) as container:
        host, port = container.get_container_host_ip(), container.get_exposed_port(8080)
        wiremock = WireMock(f"http://{host}:{port}")
        wait_until(wiremock.is_ready, timeout=60, what="WireMock start")
        yield wiremock
        wiremock.close()


@pytest.fixture
def notifygw(wiremock_server: WireMock) -> WireMock:
    """NotifyGate, replaced by a stub. Starts with no stubs each test."""
    wiremock_server.reset()
    return wiremock_server


@pytest.fixture
def gateway(notifygw: WireMock) -> Iterator[NotifyGateway]:
    gateway = NotifyGateway(notifygw.base_url, GATEWAY_API_KEY, timeout=HTTP_TIMEOUT)
    yield gateway
    gateway.close()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def consumer_settings() -> ConsumerSettings:
    return ConsumerSettings(
        consumer="notifier-1",
        block_ms=0,
        idle_ms=TEST_IDLE_MS,
        max_deliveries=MAX_DELIVERIES,
    )


@pytest.fixture
def make_consumer(
    broker: redis.Redis,
    session_factory: sessionmaker[Session],
    gateway: NotifyGateway,
    consumer_settings: ConsumerSettings,
    clock: FixedClock,
) -> Callable[..., Consumer]:
    """A consumer, or several: the group is shared, the consumer names differ."""

    def make(gateway_override: Any = None, name: str = "notifier-1") -> Consumer:
        from dataclasses import replace

        consumer = Consumer(
            broker,
            session_factory,
            gateway_override if gateway_override is not None else gateway,
            replace(consumer_settings, consumer=name),
            clock=clock,
        )
        consumer.ensure_group()
        return consumer

    return make


@pytest.fixture
def consumer(make_consumer: Callable[..., Consumer]) -> Consumer:
    return make_consumer()


@pytest.fixture
def publish(broker: redis.Redis, consumer_settings: ConsumerSettings) -> Callable[..., str]:
    """Put an event into the stream the way the booking relay does."""

    def put(**overrides: Any) -> str:
        event = booking_event(**overrides)
        broker.xadd(
            consumer_settings.stream,
            {"topic": str(event["type"]), "data": json.dumps(event)},
        )
        return str(event["event_id"])

    return put


def booking_event(**overrides: Any) -> dict[str, Any]:
    """An event exactly as booking publishes it (contracts/events/booking.v1.json)."""
    event = {
        "event_id": str(uuid.uuid7()),
        "type": "booking.confirmed",
        "version": 1,
        "occurred_at": NOW.isoformat(),
        "booking_id": str(uuid.uuid7()),
        "slot_id": str(uuid.uuid7()),
        "client_id": "client-1",
        "starts_at": STARTS_AT.isoformat(),
    }
    return event | overrides


def stub_gateway_accepts(notifygw: WireMock, message_id: str = "msg_1") -> None:
    notifygw.stub("POST", MESSAGES, status=201, json_body=gateway_message(message_id))


def gateway_message(message_id: str = "msg_1") -> dict[str, str]:
    """NotifyGate's answer. Checked against its schema (test_notifygw_contract)."""
    return {"id": message_id, "status": "queued"}


def notifications(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT event_id, booking_id, client_id, event_type, text, status,"
                " attempts, last_error, gateway_message_id FROM notifications"
                " ORDER BY created_at, event_id"
            )
        ).mappings()
        return [dict(row) for row in rows]


@pytest.fixture
def app(database_url: str, engine: Engine) -> FastAPI:
    return create_app(Settings(database_url=database_url))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client
