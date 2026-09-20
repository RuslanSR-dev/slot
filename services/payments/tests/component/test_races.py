"""Duplicates that arrive at the same moment (ADR-0008).

Sequential repeats are easy. The database must also resolve repeats that
race each other: all requests in a round are released through a barrier.
"""

import threading
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from .conftest import (
    CONFIRM,
    create_payment,
    outbox,
    paystub_event,
    send_webhook,
    stub_booking_confirm,
    stub_charge_created,
)
from .wiremock import WireMock

CONCURRENT_REQUESTS = 15


def all_at_once(base_url: str, call: Callable[[httpx2.Client], Any]) -> list[Any]:
    barrier = threading.Barrier(CONCURRENT_REQUESTS)

    def one(_: int) -> Any:
        with httpx2.Client(base_url=base_url, timeout=30) as http:
            barrier.wait()
            return call(http)

    with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as pool:
        return list(pool.map(one, range(CONCURRENT_REQUESTS)))


def test_concurrent_requests_for_one_booking_create_one_payment(
    live_server: str, stubs: WireMock, engine: Engine
) -> None:
    stub_charge_created(stubs)
    booking_id = uuid.UUID(int=42)

    responses = all_at_once(live_server, lambda http: create_payment(http, booking_id))

    statuses = Counter(r.status_code for r in responses)
    assert statuses[201] == 1, f"exactly one request creates the payment: {dict(statuses)}"
    assert statuses[200] == CONCURRENT_REQUESTS - 1
    assert len({r.json()["id"] for r in responses}) == 1
    with engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM payments")).scalar_one()
    assert count == 1


def test_concurrent_copies_of_one_event_confirm_the_booking_once(
    client: TestClient,
    live_server: str,
    stubs: WireMock,
    engine: Engine,
    relay: Callable[[], int],
) -> None:
    """Simultaneous duplicates are separated by the primary key, not by luck.

    Since ADR-0010 the winner does not call booking inside its transaction,
    so what the losers must not do is write a second command.
    """
    stub_charge_created(stubs)
    payment = create_payment(client, uuid.UUID(int=42)).json()
    stub_booking_confirm(stubs)
    event = paystub_event(payment["id"])

    responses = all_at_once(live_server, lambda http: send_webhook(http, event))

    outcomes = Counter(r.json()["outcome"] for r in responses)
    assert outcomes == Counter({"processed": 1, "duplicate": CONCURRENT_REQUESTS - 1})
    assert [command["topic"] for command in outbox(engine)] == ["booking.confirm"]

    relay()

    assert len(stubs.received("POST", CONFIRM, pattern=True)) == 1
