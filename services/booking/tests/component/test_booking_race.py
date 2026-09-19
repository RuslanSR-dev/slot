"""The double-booking race (ADR-0005).

Sequential tests cannot see this bug: it needs requests that arrive at the
same moment. Every round releases all requests at once through a barrier,
and the race is repeated several times because a single round can miss it.
"""

import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from booking.domain import ACTIVE_STATUSES

ROUNDS = 10
CONCURRENT_REQUESTS = 20


def book_concurrently(base_url: str, slot_id: str, requests: int) -> list[int]:
    barrier = threading.Barrier(requests)

    def book(client_number: int) -> int:
        with httpx2.Client(base_url=base_url, timeout=30) as http:
            barrier.wait()
            response = http.post(
                "/bookings", json={"slot_id": slot_id, "client_id": f"client-{client_number}"}
            )
            return response.status_code

    with ThreadPoolExecutor(max_workers=requests) as pool:
        return list(pool.map(book, range(requests)))


def active_bookings(engine: Engine, slot_id: str) -> int:
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM bookings WHERE slot_id = :slot AND status = ANY(:active)"),
            {"slot": slot_id, "active": [str(s) for s in ACTIVE_STATUSES]},
        ).scalar_one()
    return int(count)


def test_only_one_of_concurrent_bookings_wins(
    client: TestClient, live_server: str, engine: Engine, clock: Callable[[], datetime]
) -> None:
    now = clock()
    broken_rounds = []
    for round_number in range(ROUNDS):
        slot = client.post(
            "/slots",
            json={
                "master_id": "anna",
                "starts_at": (now + timedelta(hours=round_number + 1)).isoformat(),
                "ends_at": (now + timedelta(hours=round_number + 2)).isoformat(),
                "price_minor": 150_000,
            },
        ).json()

        statuses = Counter(book_concurrently(live_server, slot["id"], CONCURRENT_REQUESTS))
        in_database = active_bookings(engine, slot["id"])

        if statuses != Counter({201: 1, 409: CONCURRENT_REQUESTS - 1}) or in_database != 1:
            broken_rounds.append(
                f"round {round_number}: responses {dict(statuses)}, "
                f"active bookings in database {in_database}"
            )

    assert not broken_rounds, (
        f"{len(broken_rounds)} of {ROUNDS} rounds let more than one client book the slot:\n"
        + "\n".join(broken_rounds)
    )
