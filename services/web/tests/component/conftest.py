"""Component level: the web service with booking replaced by WireMock (ADR-0006).

Web has no database of its own - its whole job is to turn what booking says
into a page and back. So its component level is the real HTTP boundary with
booking: real requests, real status codes, real network failures.
"""

import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from testcontainers.core.container import DockerContainer

from web.app import NAME_COOKIE, ROLE_COOKIE, SESSION_COOKIE, Settings, create_app

from .wiremock import WIREMOCK_IMAGE, WireMock

AUTH_SECRET = "component-test-secret"
CLIENT_NAME = "client-1"
MASTER_NAME = "anna"
# Short, so a test of a hanging neighbour takes a fraction of a second.
BOOKING_TIMEOUT = 0.5


def wait_until(condition: Callable[[], bool], timeout: float, what: str) -> None:
    """Poll a condition instead of sleeping a fixed time."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} did not happen within {timeout}s")
        time.sleep(0.01)


@pytest.fixture(scope="session")
def wiremock_server() -> Iterator[WireMock]:
    with DockerContainer(WIREMOCK_IMAGE).with_exposed_ports(8080) as container:
        host, port = container.get_container_host_ip(), container.get_exposed_port(8080)
        wiremock = WireMock(f"http://{host}:{port}")
        wait_until(wiremock.is_ready, timeout=60, what="WireMock start")
        yield wiremock
        wiremock.close()


@pytest.fixture
def booking_stub(wiremock_server: WireMock) -> WireMock:
    """The booking service, replaced by a stub. Starts with no stubs each test."""
    wiremock_server.reset()
    return wiremock_server


@pytest.fixture
def app(booking_stub: WireMock) -> FastAPI:
    settings = Settings(
        booking_url=booking_stub.base_url,
        auth_secret=AUTH_SECRET,
        booking_timeout=BOOKING_TIMEOUT,
    )
    return create_app(settings)


@pytest.fixture
def anonymous(app: FastAPI) -> Iterator[TestClient]:
    """A browser with no cookies. Redirects are not followed: they are the behaviour."""
    with TestClient(app, follow_redirects=False) as client:
        yield client


def sign_in(client: TestClient, name: str, role: str) -> None:
    """Log in through the real form: the cookies are the ones a browser would get."""
    response = client.post("/login", data={"name": name, "role": role})
    assert response.status_code == 303, response.text
    assert client.cookies.get(SESSION_COOKIE), "a session must be set"
    assert client.cookies.get(NAME_COOKIE) == name
    assert client.cookies.get(ROLE_COOKIE) == role


@pytest.fixture
def client(anonymous: TestClient) -> TestClient:
    sign_in(anonymous, CLIENT_NAME, "client")
    return anonymous


@pytest.fixture
def master(anonymous: TestClient) -> TestClient:
    sign_in(anonymous, MASTER_NAME, "master")
    return anonymous


def slot_row(
    slot_id: str = "01999999-0000-7000-8000-000000000001",
    starts_at: str = "2026-09-21T09:00:00+00:00",
    available: bool = True,
    price_minor: int = 150_000,
) -> dict[str, Any]:
    return {
        "id": slot_id,
        "master_id": MASTER_NAME,
        "starts_at": starts_at,
        "ends_at": "2026-09-21T10:00:00+00:00",
        "price_minor": price_minor,
        "available": available,
    }


def booking_row(
    booking_id: str = "01999999-0000-7000-8000-000000000002",
    state: str = "pending",
) -> dict[str, Any]:
    return {
        "id": booking_id,
        "slot_id": "01999999-0000-7000-8000-000000000001",
        "client_id": CLIENT_NAME,
        "master_id": MASTER_NAME,
        "status": state,
        "starts_at": "2026-09-21T09:00:00+00:00",
        "ends_at": "2026-09-21T10:00:00+00:00",
        "price_minor": 150_000,
        "created_at": "2026-09-20T12:00:00+00:00",
        "updated_at": "2026-09-20T12:00:00+00:00",
    }


def error(code: str) -> dict[str, str]:
    """The error shape of every Slot API (docs/test-strategy.md, rule 19)."""
    return {"error": code, "detail": "whatever humans read"}
