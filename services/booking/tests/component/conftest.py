"""Component level: the service with a real Postgres, nothing else (ADR-0006)."""

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from testcontainers.community.postgres import PostgresContainer

from booking import migrate
from booking.app import Settings, create_app

# Same image as compose.yaml: tests must run against the database we ship with.
POSTGRES_IMAGE = "postgres:18-alpine"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


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
            connection.execute(text("TRUNCATE bookings, slots"))


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def app(database_url: str, engine: Engine, clock: FixedClock) -> FastAPI:
    # A pool larger than the race test's concurrency, so requests do not
    # queue for connections and actually hit the database at the same time.
    return create_app(Settings(database_url=database_url, db_pool_size=25), clock=clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
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
