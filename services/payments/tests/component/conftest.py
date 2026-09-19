"""Component level: payments with a real Postgres. PayStub and booking are
WireMock stubs on one server: PayStub under /v1, booking under /internal."""

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

from payments import migrate
from payments.app import Settings, create_app
from payments.domain import sign

from .wiremock import WIREMOCK_IMAGE, WireMock

# Same image as compose.yaml: tests must run against the database we ship with.
POSTGRES_IMAGE = "postgres:18-alpine"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
WEBHOOK_SECRET = "test-webhook-secret"
PAYSTUB_API_KEY = "test-api-key"
# Short, so tests of a hanging neighbour take a fraction of a second.
HTTP_TIMEOUT = 0.5
CHARGES = "/v1/charges"
CONFIRM = r"/internal/bookings/[0-9a-f-]+/confirm"


def wait_until(condition: Callable[[], bool], timeout: float, what: str) -> None:
    """Poll a condition instead of sleeping a fixed time."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} did not happen within {timeout}s")
        time.sleep(0.01)


@dataclass
class FixedClock:
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
    uses_database = "database_url" in request.fixturenames
    engine: Engine | None = request.getfixturevalue("engine") if uses_database else None
    yield
    if engine is not None:
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE payments, provider_events"))


@pytest.fixture(scope="session")
def wiremock_server() -> Iterator[WireMock]:
    with DockerContainer(WIREMOCK_IMAGE).with_exposed_ports(8080) as container:
        host, port = container.get_container_host_ip(), container.get_exposed_port(8080)
        wiremock = WireMock(f"http://{host}:{port}")
        wait_until(wiremock.is_ready, timeout=60, what="WireMock start")
        yield wiremock
        wiremock.close()


@pytest.fixture
def stubs(wiremock_server: WireMock) -> WireMock:
    """PayStub and booking, replaced by stubs. Starts with no stubs each test."""
    wiremock_server.reset()
    return wiremock_server


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def app(database_url: str, engine: Engine, clock: FixedClock, stubs: WireMock) -> FastAPI:
    settings = Settings(
        database_url=database_url,
        paystub_url=stubs.base_url,
        paystub_api_key=PAYSTUB_API_KEY,
        webhook_secret=WEBHOOK_SECRET,
        booking_url=stubs.base_url,
        http_timeout=HTTP_TIMEOUT,
        db_pool_size=25,
    )
    return create_app(settings, clock=clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


@pytest.fixture
def live_server(app: FastAPI) -> Iterator[str]:
    """The app served by a real HTTP server: concurrency tests need one."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until(lambda: server.started, timeout=10, what="server start")
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def stub_charge_created(stubs: WireMock, charge_id: str = "ch_1") -> None:
    stubs.stub(
        "POST",
        CHARGES,
        status=201,
        json_body={
            "id": charge_id,
            "status": "requires_payment",
            "checkout_url": f"https://paystub.example/checkout/{charge_id}",
        },
    )


def stub_booking_confirm(stubs: WireMock, **response: Any) -> None:
    stubs.stub("POST", CONFIRM, pattern=True, **({"status": 200} | response))


def create_payment(
    http: TestClient | httpx2.Client, booking_id: uuid.UUID, amount_minor: int = 150_000
) -> Any:
    return http.post(
        "/payments",
        json={"booking_id": str(booking_id), "amount_minor": amount_minor, "currency": "RUB"},
    )


def paystub_event(payment_id: str, event_type: str = "charge.succeeded", **overrides: Any) -> Any:
    event = {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": event_type,
        "data": {"charge_id": "ch_1", "reference": payment_id},
    }
    return event | overrides


def send_webhook(
    http: TestClient | httpx2.Client, event: Any, signature: str | None = "sign"
) -> Any:
    """Send an event the way PayStub does: raw JSON body, HMAC over exactly these bytes."""
    body = json.dumps(event).encode()
    headers = {"Content-Type": "application/json"}
    if signature == "sign":
        headers["PayStub-Signature"] = sign(body, WEBHOOK_SECRET)
    elif signature is not None:
        headers["PayStub-Signature"] = signature
    return http.post("/webhooks/paystub", content=body, headers=headers)
