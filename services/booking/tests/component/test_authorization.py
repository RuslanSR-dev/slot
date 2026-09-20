"""Who may touch what, checked at the API level.

This is the level that matters: a rule enforced only by hiding a button in
the interface is not a rule. Every test here talks to the API directly,
the way a curl, a mobile client or a stranger would.
"""

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import DEFAULT_CLIENT, DEFAULT_MASTER, NOW, FixedClock, auth
from .test_bookings_api import create_slot

OTHER_CLIENT = "client-2"
OTHER_MASTER = "boris"
UNKNOWN_BOOKING = "00000000-0000-7000-8000-000000000000"


@pytest.fixture
def booking(client: TestClient) -> Any:
    """A booking of the default client on a slot of the default master."""
    slot = create_slot(client)
    created = client.post("/bookings", json={"slot_id": slot["id"]})
    assert created.status_code == 201, created.text
    return created.json()


def slot_body() -> dict[str, Any]:
    return {
        "starts_at": (NOW + timedelta(hours=5)).isoformat(),
        "ends_at": (NOW + timedelta(hours=6)).isoformat(),
        "price_minor": 150_000,
    }


class TestWithoutAToken:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            pytest.param("POST", "/slots", slot_body(), id="publish-a-slot"),
            pytest.param("POST", "/bookings", {"slot_id": UNKNOWN_BOOKING}, id="book"),
            pytest.param("GET", "/bookings", None, id="list-my-bookings"),
            pytest.param("GET", f"/bookings/{UNKNOWN_BOOKING}", None, id="read-a-booking"),
            pytest.param("DELETE", f"/bookings/{UNKNOWN_BOOKING}", None, id="cancel"),
            pytest.param(
                "POST", f"/bookings/{UNKNOWN_BOOKING}/payment", None, id="start-a-payment"
            ),
        ],
    )
    def test_a_request_without_a_token_is_refused(
        self, anonymous: TestClient, method: str, path: str, body: Any
    ) -> None:
        response = anonymous.request(method, path, json=body)

        assert response.status_code == 401, response.text
        assert response.json()["error"] == "unauthorized"

    def test_the_refusal_happens_before_anything_is_read(
        self, anonymous: TestClient, booking: Any
    ) -> None:
        """A 401 must not depend on whether the booking exists: no oracle for ids."""
        unknown = anonymous.get(f"/bookings/{UNKNOWN_BOOKING}")
        existing = anonymous.get(f"/bookings/{booking['id']}")

        assert unknown.status_code == existing.status_code == 401
        assert unknown.json() == existing.json()


class TestWithABrokenToken:
    @pytest.mark.parametrize(
        ("header", "expected_error"),
        [
            pytest.param("Bearer not-a-token", "invalid_token", id="nonsense"),
            pytest.param("Bearer v1.payload.signature", "invalid_token", id="made-up-parts"),
            pytest.param("Basic dXNlcjpwYXNz", "unauthorized", id="another-scheme"),
        ],
    )
    def test_a_token_we_did_not_sign_is_refused(
        self, anonymous: TestClient, header: str, expected_error: str
    ) -> None:
        response = anonymous.get("/bookings", headers={"Authorization": header})

        assert response.status_code == 401
        assert response.json()["error"] == expected_error

    def test_a_token_with_a_forged_signature_is_refused(
        self, anonymous: TestClient, booking: Any
    ) -> None:
        """The claims say "client-1", the signature does not say we wrote them."""
        version, payload, _ = (
            auth(DEFAULT_CLIENT)["Authorization"].removeprefix("Bearer ").split(".")
        )
        forged = f"{version}.{payload}.{'A' * 43}="

        response = anonymous.get(
            f"/bookings/{booking['id']}", headers={"Authorization": f"Bearer {forged}"}
        )

        assert response.status_code == 401
        assert response.json()["error"] == "invalid_token"

    def test_an_expired_token_is_refused_and_says_so(
        self, anonymous: TestClient, booking: Any, clock: FixedClock
    ) -> None:
        """The clock moves past the expiry, not the test: no waiting for real time."""
        yesterday = NOW - timedelta(days=1)
        headers = auth(DEFAULT_CLIENT, expires_at=yesterday)

        response = anonymous.get(f"/bookings/{booking['id']}", headers=headers)

        assert response.status_code == 401
        assert response.json()["error"] == "token_expired", "the client must know to log in again"

    def test_a_token_that_is_still_valid_works(self, anonymous: TestClient, booking: Any) -> None:
        """The counterpart of the test above: otherwise the expiry proves nothing."""
        headers = auth(DEFAULT_CLIENT, expires_at=NOW + timedelta(seconds=1))

        assert anonymous.get(f"/bookings/{booking['id']}", headers=headers).status_code == 200


class TestSomeoneElsesBooking:
    @pytest.mark.parametrize(
        "stranger",
        [
            pytest.param(auth(OTHER_CLIENT), id="another-client"),
            pytest.param(auth(OTHER_MASTER, "master"), id="another-master"),
        ],
    )
    def test_a_stranger_cannot_read_it(
        self, anonymous: TestClient, booking: Any, stranger: dict[str, str]
    ) -> None:
        response = anonymous.get(f"/bookings/{booking['id']}", headers=stranger)

        assert response.status_code == 404
        assert response.json()["error"] == "booking_not_found"

    @pytest.mark.parametrize(
        "stranger",
        [
            pytest.param(auth(OTHER_CLIENT), id="another-client"),
            pytest.param(auth(OTHER_MASTER, "master"), id="another-master"),
        ],
    )
    def test_a_stranger_cannot_cancel_it(
        self, anonymous: TestClient, client: TestClient, booking: Any, stranger: dict[str, str]
    ) -> None:
        response = anonymous.delete(f"/bookings/{booking['id']}", headers=stranger)

        assert response.status_code == 404
        assert response.json()["error"] == "booking_not_found"
        # The refusal is what matters, not the code: the booking must be untouched.
        assert client.get(f"/bookings/{booking['id']}").json()["status"] == "pending"

    def test_a_stranger_cannot_pay_for_it(self, anonymous: TestClient, booking: Any) -> None:
        response = anonymous.post(f"/bookings/{booking['id']}/payment", headers=auth(OTHER_CLIENT))

        assert response.status_code == 404
        assert response.json()["error"] == "booking_not_found"

    def test_a_stranger_gets_the_same_answer_as_for_a_booking_that_does_not_exist(
        self, anonymous: TestClient, booking: Any
    ) -> None:
        """404, not 403. A 403 would confirm that this booking id exists."""
        someone_elses = anonymous.get(f"/bookings/{booking['id']}", headers=auth(OTHER_CLIENT))
        never_existed = anonymous.get(f"/bookings/{UNKNOWN_BOOKING}", headers=auth(OTHER_CLIENT))

        assert someone_elses.status_code == never_existed.status_code
        assert someone_elses.json()["error"] == never_existed.json()["error"]


class TestBothSidesOfABooking:
    def test_the_client_sees_their_booking(self, client: TestClient, booking: Any) -> None:
        assert client.get(f"/bookings/{booking['id']}").json()["id"] == booking["id"]

    def test_the_master_of_the_slot_sees_the_booking(
        self, anonymous: TestClient, booking: Any
    ) -> None:
        response = anonymous.get(
            f"/bookings/{booking['id']}", headers=auth(DEFAULT_MASTER, "master")
        )

        assert response.status_code == 200
        assert response.json()["client_id"] == DEFAULT_CLIENT

    def test_the_master_of_the_slot_can_cancel_the_booking(
        self, anonymous: TestClient, booking: Any
    ) -> None:
        """A master manages their own slots: a client who does not come can be cancelled."""
        response = anonymous.delete(
            f"/bookings/{booking['id']}", headers=auth(DEFAULT_MASTER, "master")
        )

        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"


class TestRoles:
    def test_a_client_cannot_publish_slots(self, client: TestClient) -> None:
        response = client.post("/slots", json=slot_body())

        assert response.status_code == 403
        assert response.json()["error"] == "forbidden"

    def test_a_master_cannot_book_a_slot(self, anonymous: TestClient, client: TestClient) -> None:
        slot = create_slot(client)

        response = anonymous.post(
            "/bookings", json={"slot_id": slot["id"]}, headers=auth(DEFAULT_MASTER, "master")
        )

        assert response.status_code == 403
        assert response.json()["error"] == "forbidden"

    def test_a_master_cannot_start_a_payment(self, anonymous: TestClient, booking: Any) -> None:
        """Paying is the client's business, even for a booking on the master's own slot."""
        response = anonymous.post(
            f"/bookings/{booking['id']}/payment", headers=auth(DEFAULT_MASTER, "master")
        )

        assert response.status_code == 403
        assert response.json()["error"] == "forbidden"

    def test_a_master_publishes_slots_under_their_own_name_only(
        self, anonymous: TestClient
    ) -> None:
        """There is no way to say whose slot it is: the token decides."""
        response = anonymous.post(
            "/slots",
            json=slot_body() | {"master_id": OTHER_MASTER},
            headers=auth(DEFAULT_MASTER, "master"),
        )

        assert response.status_code == 201
        assert response.json()["master_id"] == DEFAULT_MASTER


class TestMyBookings:
    def test_a_client_sees_only_their_own(
        self, anonymous: TestClient, client: TestClient, booking: Any
    ) -> None:
        other_slot = create_slot(client, hours_from_now=3)
        anonymous.post("/bookings", json={"slot_id": other_slot["id"]}, headers=auth(OTHER_CLIENT))

        mine = client.get("/bookings").json()

        assert [row["id"] for row in mine] == [booking["id"]]
        assert mine[0]["client_id"] == DEFAULT_CLIENT

    def test_a_booking_comes_with_the_slot_behind_it(
        self, client: TestClient, booking: Any
    ) -> None:
        """One page, one request: the interface must not read slots it has no right to."""
        [mine] = client.get("/bookings").json()

        assert mine["master_id"] == DEFAULT_MASTER
        assert mine["price_minor"] == 150_000
        assert mine["starts_at"].startswith("2026-09-20T13:00:00")

    def test_a_master_sees_the_bookings_on_their_slots(
        self, anonymous: TestClient, booking: Any
    ) -> None:
        mine = anonymous.get("/bookings", headers=auth(DEFAULT_MASTER, "master")).json()

        assert [row["id"] for row in mine] == [booking["id"]]

    def test_another_master_sees_nothing(self, anonymous: TestClient, booking: Any) -> None:
        assert anonymous.get("/bookings", headers=auth(OTHER_MASTER, "master")).json() == []


def test_the_internal_endpoint_is_not_guarded_by_a_token(
    anonymous: TestClient, booking: Any
) -> None:
    """A deliberate gap: payments calls it inside the network, with no token of its own.

    Written down as a test so that it is a decision, not an oversight: the
    day the network stops being trusted, this test is the one that changes.
    """
    response = anonymous.post(f"/internal/bookings/{booking['id']}/confirm")

    assert response.status_code == 200
