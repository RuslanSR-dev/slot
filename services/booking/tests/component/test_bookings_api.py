from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import NOW, FixedClock

HOUR = timedelta(hours=1)


def create_slot(client: TestClient, master_id: str = "anna", hours_from_now: int = 1) -> Any:
    response = client.post(
        "/slots",
        json={
            "master_id": master_id,
            "starts_at": (NOW + hours_from_now * HOUR).isoformat(),
            "ends_at": (NOW + (hours_from_now + 1) * HOUR).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def book(client: TestClient, slot_id: str, client_id: str = "client-1") -> Any:
    return client.post("/bookings", json={"slot_id": slot_id, "client_id": client_id})


class TestSlots:
    def test_new_slot_is_available(self, client: TestClient) -> None:
        slot = create_slot(client)

        listed = client.get("/slots", params={"master_id": "anna"}).json()

        assert [s["id"] for s in listed] == [slot["id"]]
        assert listed[0]["available"] is True

    @pytest.mark.parametrize(
        ("starts_at", "ends_at"),
        [
            pytest.param(NOW + 2 * HOUR, NOW + HOUR, id="ends-before-start"),
            pytest.param(NOW - HOUR, NOW + HOUR, id="starts-in-past"),
        ],
    )
    def test_invalid_slot_is_rejected(
        self, client: TestClient, starts_at: Any, ends_at: Any
    ) -> None:
        response = client.post(
            "/slots",
            json={
                "master_id": "anna",
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
            },
        )

        assert response.status_code == 422
        assert response.json()["error"] == "invalid_slot"

    def test_slot_time_without_timezone_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/slots",
            json={
                "master_id": "anna",
                "starts_at": "2026-09-21T10:00:00",
                "ends_at": "2026-09-21T11:00:00",
            },
        )

        assert response.status_code == 422

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
        listed = client.get("/slots", params={"master_id": "anna"}).json()
        assert listed[0]["available"] is False

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
        listed = client.get("/slots", params={"master_id": "anna"}).json()
        assert listed[0]["available"] is False

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

    def test_cancelled_booking_cannot_be_cancelled_again(self, client: TestClient) -> None:
        slot = create_slot(client)
        booking = book(client, slot["id"]).json()
        client.delete(f"/bookings/{booking['id']}")

        response = client.delete(f"/bookings/{booking['id']}")

        assert response.status_code == 409
        assert response.json()["error"] == "invalid_transition"

    def test_cancelled_booking_cannot_be_confirmed(self, client: TestClient) -> None:
        slot = create_slot(client)
        booking = book(client, slot["id"]).json()
        client.delete(f"/bookings/{booking['id']}")

        response = client.post(f"/internal/bookings/{booking['id']}/confirm")

        assert response.status_code == 409
        assert response.json()["error"] == "invalid_transition"
