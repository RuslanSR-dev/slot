from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import NOW, PENDING_TTL, FixedClock

HOUR = timedelta(hours=1)
PRICE = 150_000


def create_slot(client: TestClient, master_id: str = "anna", hours_from_now: int = 1) -> Any:
    response = client.post(
        "/slots",
        json={
            "master_id": master_id,
            "starts_at": (NOW + hours_from_now * HOUR).isoformat(),
            "ends_at": (NOW + (hours_from_now + 1) * HOUR).isoformat(),
            "price_minor": PRICE,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def book(client: TestClient, slot_id: str, client_id: str = "client-1") -> Any:
    return client.post("/bookings", json={"slot_id": slot_id, "client_id": client_id})


def slot_is_available(client: TestClient, slot_id: str) -> bool:
    listed = client.get("/slots", params={"master_id": "anna"}).json()
    return bool(next(s["available"] for s in listed if s["id"] == slot_id))


class TestSlots:
    def test_new_slot_is_available_with_its_price(self, client: TestClient) -> None:
        slot = create_slot(client)

        listed = client.get("/slots", params={"master_id": "anna"}).json()

        assert [s["id"] for s in listed] == [slot["id"]]
        assert listed[0]["available"] is True
        assert listed[0]["price_minor"] == PRICE

    @pytest.mark.parametrize(
        ("starts_at", "ends_at"),
        [
            pytest.param(NOW + 2 * HOUR, NOW + HOUR, id="ends-before-start"),
            pytest.param(NOW - HOUR, NOW + HOUR, id="starts-in-past"),
        ],
    )
    def test_invalid_slot_is_rejected(
        self, client: TestClient, starts_at: datetime, ends_at: datetime
    ) -> None:
        response = client.post(
            "/slots",
            json={
                "master_id": "anna",
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
                "price_minor": PRICE,
            },
        )

        assert response.status_code == 422
        assert response.json()["error"] == "invalid_slot"

    @pytest.mark.parametrize(
        "body_change",
        [
            pytest.param({"starts_at": "2026-09-21T10:00:00"}, id="time-without-timezone"),
            pytest.param({"price_minor": 0}, id="zero-price"),
            pytest.param({"price_minor": None}, id="no-price"),
        ],
    )
    def test_malformed_slot_is_rejected(
        self, client: TestClient, body_change: dict[str, Any]
    ) -> None:
        body = {
            "master_id": "anna",
            "starts_at": (NOW + HOUR).isoformat(),
            "ends_at": (NOW + 2 * HOUR).isoformat(),
            "price_minor": PRICE,
        } | body_change

        assert client.post("/slots", json=body).status_code == 422

    def test_slots_are_filtered_by_master_and_day(self, client: TestClient) -> None:
        today = create_slot(client, hours_from_now=1)
        create_slot(client, hours_from_now=25)  # tomorrow
        create_slot(client, master_id="boris", hours_from_now=2)

        listed = client.get(
            "/slots", params={"master_id": "anna", "date": NOW.date().isoformat()}
        ).json()

        assert [s["id"] for s in listed] == [today["id"]]


class TestBooking:
    def test_booking_takes_the_slot(self, client: TestClient) -> None:
        slot = create_slot(client)

        response = book(client, slot["id"])

        assert response.status_code == 201
        assert response.json()["status"] == "pending"
        assert slot_is_available(client, slot["id"]) is False

    def test_second_booking_of_the_same_slot_is_a_conflict(self, client: TestClient) -> None:
        slot = create_slot(client)
        book(client, slot["id"], "client-1")

        response = book(client, slot["id"], "client-2")

        assert response.status_code == 409
        assert response.json()["error"] == "slot_already_booked"

    def test_unknown_slot_cannot_be_booked(self, client: TestClient) -> None:
        response = book(client, "00000000-0000-7000-8000-000000000000")

        assert response.status_code == 404
        assert response.json()["error"] == "slot_not_found"

    def test_slot_cannot_be_booked_once_it_started(
        self, client: TestClient, clock: FixedClock
    ) -> None:
        slot = create_slot(client, hours_from_now=1)
        clock.now = NOW + HOUR  # the slot starts right now

        response = book(client, slot["id"])

        assert response.status_code == 409
        assert response.json()["error"] == "slot_in_past"
        assert slot_is_available(client, slot["id"]) is False

    def test_unknown_booking_is_not_found(self, client: TestClient) -> None:
        response = client.get("/bookings/00000000-0000-7000-8000-000000000000")

        assert response.status_code == 404
        assert response.json()["error"] == "booking_not_found"


class TestLifecycle:
    def test_cancelled_booking_frees_the_slot_for_another_client(self, client: TestClient) -> None:
        slot = create_slot(client)
        first = book(client, slot["id"], "client-1").json()

        cancelled = client.delete(f"/bookings/{first['id']}")
        second = book(client, slot["id"], "client-2")

        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert second.status_code == 201

    def test_confirmed_booking_can_be_cancelled(self, client: TestClient) -> None:
        slot = create_slot(client)
        booking = book(client, slot["id"]).json()

        confirmed = client.post(f"/internal/bookings/{booking['id']}/confirm")
        cancelled = client.delete(f"/bookings/{booking['id']}")

        assert confirmed.json()["status"] == "confirmed"
        assert cancelled.json()["status"] == "cancelled"
        assert client.get(f"/bookings/{booking['id']}").json()["status"] == "cancelled"

    def test_cancelled_booking_cannot_be_confirmed(self, client: TestClient) -> None:
        slot = create_slot(client)
        booking = book(client, slot["id"]).json()
        client.delete(f"/bookings/{booking['id']}")

        response = client.post(f"/internal/bookings/{booking['id']}/confirm")

        assert response.status_code == 409
        assert response.json()["error"] == "invalid_transition"


class TestRetriesAreSafe:
    """A lost response makes the caller retry. The retry must not fail (ADR-0008)."""

    def test_confirming_twice_returns_the_same_booking(
        self, client: TestClient, clock: FixedClock
    ) -> None:
        slot = create_slot(client)
        booking = book(client, slot["id"]).json()
        first = client.post(f"/internal/bookings/{booking['id']}/confirm")
        clock.now = NOW + timedelta(minutes=1)

        second = client.post(f"/internal/bookings/{booking['id']}/confirm")

        assert second.status_code == 200
        assert second.json() == first.json(), "a repeat must not change the booking"

    def test_cancelling_twice_returns_the_cancelled_booking(self, client: TestClient) -> None:
        slot = create_slot(client)
        booking = book(client, slot["id"]).json()
        client.delete(f"/bookings/{booking['id']}")

        response = client.delete(f"/bookings/{booking['id']}")

        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"


class TestUnpaidBookingExpires:
    """An unpaid booking stops holding its slot after the TTL (ADR-0008)."""

    def test_pending_booking_holds_the_slot_until_the_ttl(
        self, client: TestClient, clock: FixedClock
    ) -> None:
        slot = create_slot(client)
        book(client, slot["id"], "client-1")
        clock.now = NOW + PENDING_TTL - timedelta(seconds=1)

        assert slot_is_available(client, slot["id"]) is False
        assert book(client, slot["id"], "client-2").status_code == 409

    def test_slot_is_free_again_exactly_at_the_ttl(
        self, client: TestClient, clock: FixedClock
    ) -> None:
        slot = create_slot(client)
        first = book(client, slot["id"], "client-1").json()
        clock.now = NOW + PENDING_TTL

        assert slot_is_available(client, slot["id"]) is True
        second = book(client, slot["id"], "client-2")

        assert second.status_code == 201
        assert client.get(f"/bookings/{first['id']}").json()["status"] == "expired"

    def test_confirmed_booking_never_expires(self, client: TestClient, clock: FixedClock) -> None:
        slot = create_slot(client)
        booking = book(client, slot["id"]).json()
        client.post(f"/internal/bookings/{booking['id']}/confirm")
        clock.now = NOW + 2 * PENDING_TTL

        assert slot_is_available(client, slot["id"]) is False
        assert book(client, slot["id"], "client-2").status_code == 409

    def test_expired_booking_cannot_be_confirmed(
        self, client: TestClient, clock: FixedClock
    ) -> None:
        """Payment arrived after the slot went to someone else: a known gap (ADR-0008)."""
        slot = create_slot(client)
        first = book(client, slot["id"], "client-1").json()
        clock.now = NOW + PENDING_TTL
        book(client, slot["id"], "client-2")

        response = client.post(f"/internal/bookings/{first['id']}/confirm")

        assert response.status_code == 409
        assert response.json()["error"] == "invalid_transition"
