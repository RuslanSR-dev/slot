"""Component level: the service with a real Postgres; neighbours are WireMock stubs (ADR-0006)."""

import base64
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import redis
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer

from booking import migrate
from booking.app import Settings, create_app

from .wiremock import WIREMOCK_IMAGE, WireMock

# Same images as compose.yaml: tests must run against what we ship with.
POSTGRES_IMAGE = "postgres:18-alpine"
REDIS_IMAGE = "redis:8.2-alpine"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
PENDING_TTL = timedelta(minutes=15)
DAY = timedelta(days=1)
# Short, so tests of a hanging neighbour take a fraction of a second.
PAYMENTS_TIMEOUT = 0.5
# The secret the web service would share with booking. Tokens are built here
# by hand, not by booking's own code: booking can only verify them.
AUTH_SECRET = "component-test-secret"
DEFAULT_CLIENT = "client-1"
DEFAULT_MASTER = "anna"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).replace(b"=", b"").decode()


def token(subject: str, role: str, expires_at: datetime | None = None) -> str:
    claims = {"sub": subject, "role": role, "exp": int((expires_at or NOW + DAY).timestamp())}
    payload = _b64(json.dumps(claims).encode())
    signature = _b64(
        hmac.new(AUTH_SECRET.encode(), f"v1.{payload}".encode(), hashlib.sha256).digest()
    )
    return f"v1.{payload}.{signature}"


def auth(subject: str, role: str = "client", expires_at: datetime | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(subject, role, expires_at)}"}


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
            connection.execute(text("TRUNCATE bookings, slots, outbox_messages"))


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Sessions for tests that call the service layer directly, e.g. the relay."""
    return sessionmaker(engine)


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer(REDIS_IMAGE) as container:
        yield container


@pytest.fixture
def redis_url(redis_container: RedisContainer) -> str:
    host = redis_container.get_container_host_ip()
    return f"redis://{host}:{redis_container.get_exposed_port(6379)}/0"


@pytest.fixture
def broker(redis_url: str) -> Iterator[redis.Redis]:
    """A real broker, empty at the start of every test."""
    client = redis.Redis.from_url(redis_url, decode_responses=True)
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
def payments_stub(wiremock_server: WireMock) -> WireMock:
    """The payments service, replaced by a stub. Starts with no stubs each test."""
    wiremock_server.reset()
    return wiremock_server


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def app(database_url: str, engine: Engine, clock: FixedClock, payments_stub: WireMock) -> FastAPI:
    settings = Settings(
        database_url=database_url,
        auth_secret=AUTH_SECRET,
        payments_url=payments_stub.base_url,
        payments_timeout=PAYMENTS_TIMEOUT,
        # A pool larger than the race test's concurrency, so requests do not
        # queue for connections and actually hit the database at the same time.
        db_pool_size=25,
        pending_ttl=PENDING_TTL,
    )
    return create_app(settings, clock=clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Signed in as a client: the identity most requests are made with."""
    with TestClient(app, headers=auth(DEFAULT_CLIENT)) as client:
        yield client


@pytest.fixture
def anonymous(app: FastAPI) -> Iterator[TestClient]:
    """Nobody: no token at all."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def live_server(app: FastAPI) -> Iterator[str]:
    """The app served by a real HTTP server in a background thread.

    TestClient handles requests one by one; concurrency tests need a server.
    """
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until(lambda: server.started, timeout=10, what="server start")
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
