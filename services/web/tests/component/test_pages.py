"""What the pages do with what booking answers.

Everything here is checked at the level of the HTML the browser gets, not
at the level of a rendered screen: that is what e2e is for, and there are
four of those. This level is cheap, so it is where the many cases live -
every refusal booking can send, every neighbour failure.
"""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from web.app import SESSION_COOKIE
from web.domain import explain

from .conftest import CLIENT_NAME, MASTER_NAME, booking_row, error, slot_row
from .wiremock import WireMock

SLOT_ID = "01999999-0000-7000-8000-000000000001"
BOOKING_ID = "01999999-0000-7000-8000-000000000002"
SLOT_FORM = {
    "starts_at": "2026-09-21T10:00",
    "duration_minutes": "60",
    "price": "1500",
    "timezone_offset": "0",
}


class TestLogin:
    def test_the_first_page_is_the_login_form(self, anonymous: TestClient) -> None:
        page = anonymous.get("/")

        assert page.status_code == 200
        assert 'data-testid="login"' in page.text

    def test_a_client_lands_on_their_bookings(self, anonymous: TestClient) -> None:
        response = anonymous.post("/login", data={"name": CLIENT_NAME, "role": "client"})

        assert response.status_code == 303
        assert response.headers["location"] == "/bookings"

    def test_a_master_lands_on_their_slots(self, anonymous: TestClient) -> None:
        response = anonymous.post("/login", data={"name": MASTER_NAME, "role": "master"})

        assert response.headers["location"] == "/my-slots"

    def test_the_session_cookie_is_not_readable_by_scripts(self, client: TestClient) -> None:
        """The token is the only proof of who you are: a script must not see it."""
        response = client.post("/login", data={"name": CLIENT_NAME, "role": "client"})
        session = [
            header
            for header in response.headers.get_list("set-cookie")
            if header.startswith(SESSION_COOKIE)
        ]

        assert session, "the session must be set"
        assert "httponly" in session[0].lower()

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("anna smith", id="space-inside"),
            pytest.param("", id="empty"),
            pytest.param("анна", id="not-in-the-alphabet"),
        ],
    )
    def test_a_name_we_cannot_use_is_refused_on_the_page(
        self, anonymous: TestClient, name: str
    ) -> None:
        response = anonymous.post("/login", data={"name": name, "role": "client"})

        assert response.status_code == 400
        assert 'data-testid="login"' in response.text, "the form comes back, not a traceback"

    def test_logging_out_forgets_the_session(self, client: TestClient) -> None:
        response = client.post("/logout")

        assert response.status_code == 200
        assert 'data-testid="login"' in response.text
        assert not client.cookies.get(SESSION_COOKIE)


class TestWithoutASession:
    @pytest.mark.parametrize(
        "path", ["/bookings", "/my-slots", f"/masters/{MASTER_NAME}", f"/bookings/{BOOKING_ID}/row"]
    )
    def test_a_page_of_a_signed_in_person_shows_the_login_form(
        self, anonymous: TestClient, path: str
    ) -> None:
        response = anonymous.get(path)

        assert 'data-testid="login"' in response.text

    @pytest.mark.parametrize(
        ("path", "form"),
        [
            pytest.param(f"/slots/{SLOT_ID}/book", {}, id="book"),
            # A full form on purpose: the framework validates the body before
            # our code runs, so an empty one would only prove that it does.
            pytest.param("/my-slots", SLOT_FORM, id="publish-a-slot"),
            pytest.param(f"/bookings/{BOOKING_ID}/cancel", {}, id="cancel"),
            pytest.param(f"/bookings/{BOOKING_ID}/pay", {}, id="pay"),
        ],
    )
    def test_an_action_without_a_session_reaches_nobody(
        self, anonymous: TestClient, booking_stub: WireMock, path: str, form: dict[str, str]
    ) -> None:
        """Not a redirect to think about: the answer is the form, and booking is untouched."""
        response = anonymous.post(path, data=form)

        assert 'data-testid="login"' in response.text
        assert booking_stub.received("POST", "/bookings") == []
        assert booking_stub.received("POST", "/slots") == []

    def test_a_session_booking_no_longer_accepts_ends_on_the_login_page(
        self, client: TestClient, booking_stub: WireMock
    ) -> None:
        """Web does not verify tokens - it finds out from booking that ours is stale."""
        booking_stub.stub("GET", "/bookings", status=401, json_body=error("token_expired"))

        page = client.get("/bookings")

        assert "Сессия истекла" in page.text
        assert not client.cookies.get(SESSION_COOKIE), "a dead session must not stay in the browser"


class TestMyBookings:
    def test_an_empty_list_says_so(self, client: TestClient, booking_stub: WireMock) -> None:
        booking_stub.stub("GET", "/bookings", json_body=[])

        page = client.get("/bookings")

        assert 'data-testid="no-bookings"' in page.text

    def test_a_booking_is_shown_with_its_slot(
        self, client: TestClient, booking_stub: WireMock
    ) -> None:
        booking_stub.stub("GET", "/bookings", json_body=[booking_row()])

        page = client.get("/bookings")

        assert MASTER_NAME in page.text
        assert "1500.00" in page.text, "kopecks are an implementation detail, not a price"
        assert "2026-09-21T09:00:00+00:00" in page.text, "the browser turns UTC into local time"

    def test_the_token_of_the_person_looking_is_the_one_sent_on(
        self, client: TestClient, booking_stub: WireMock
    ) -> None:
        """Web never asks booking for somebody else's data: it can only pass a token."""
        booking_stub.stub("GET", "/bookings", json_body=[])

        client.get("/bookings")

        [sent] = booking_stub.received("GET", "/bookings")
        assert sent["headers"]["Authorization"].startswith("Bearer v1.")

    @pytest.mark.parametrize(
        ("state", "text"),
        [
            pytest.param("pending", "Ждём оплату", id="pending"),
            pytest.param("confirmed", "Оплачено", id="confirmed"),
            pytest.param("cancelled", "Отменена", id="cancelled"),
            pytest.param("expired", "Истекла", id="expired"),
        ],
    )
    def test_every_state_has_words_for_it(
        self, client: TestClient, booking_stub: WireMock, state: str, text: str
    ) -> None:
        booking_stub.stub("GET", "/bookings", json_body=[booking_row(state=state)])

        page = client.get("/bookings")

        assert text in page.text

    @pytest.mark.parametrize(
        ("state", "polls"),
        [
            pytest.param("pending", True, id="pending-keeps-asking"),
            pytest.param("confirmed", False, id="confirmed-stops"),
            pytest.param("cancelled", False, id="cancelled-stops"),
            pytest.param("expired", False, id="expired-stops"),
        ],
    )
    def test_the_page_asks_again_only_while_something_can_still_change(
        self, client: TestClient, booking_stub: WireMock, state: str, polls: bool
    ) -> None:
        """Polling that never stops is a load generator, not a feature."""
        booking_stub.stub("GET", "/bookings", json_body=[booking_row(state=state)])

        page = client.get("/bookings")

        assert ("hx-trigger" in page.text) is polls


class TestBookingASlot:
    def test_a_free_slot_can_be_booked(self, client: TestClient, booking_stub: WireMock) -> None:
        booking_stub.stub("GET", "/slots", json_body=[slot_row()])

        page = client.get(f"/masters/{MASTER_NAME}")

        assert 'data-testid="book"' in page.text

    def test_a_taken_slot_has_no_button(self, client: TestClient, booking_stub: WireMock) -> None:
        booking_stub.stub("GET", "/slots", json_body=[slot_row(available=False)])

        page = client.get(f"/masters/{MASTER_NAME}")

        assert 'data-testid="book"' not in page.text
        assert 'data-testid="taken"' in page.text

    def test_booking_answers_with_a_piece_of_the_page(
        self, client: TestClient, booking_stub: WireMock
    ) -> None:
        booking_stub.stub("POST", "/bookings", status=201, json_body=booking_row())

        response = client.post(f"/slots/{SLOT_ID}/book")

        assert response.status_code == 200
        assert 'data-testid="booked"' in response.text

    def test_a_slot_taken_a_moment_ago_becomes_a_sentence_not_a_code(
        self, client: TestClient, booking_stub: WireMock
    ) -> None:
        """The race of ADR-0005 as a person meets it: someone was faster."""
        booking_stub.stub("POST", "/bookings", status=409, json_body=error("slot_already_booked"))

        response = client.post(f"/slots/{SLOT_ID}/book")

        assert response.status_code == 200, "HTMX swaps successful answers; 409 would show nothing"
        assert explain("slot_already_booked") in response.text
        assert "slot_already_booked" not in response.text, "an error code is not an answer"

    @pytest.mark.parametrize(
        ("status_code", "code"),
        [
            pytest.param(404, "slot_not_found", id="slot-is-gone"),
            pytest.param(409, "slot_in_past", id="slot-has-started"),
            pytest.param(403, "forbidden", id="a-master-clicking-book"),
            pytest.param(422, "invalid_request", id="nonsense"),
        ],
    )
    def test_every_refusal_ends_up_as_readable_text(
        self, client: TestClient, booking_stub: WireMock, status_code: int, code: str
    ) -> None:
        booking_stub.stub("POST", "/bookings", status=status_code, json_body=error(code))

        response = client.post(f"/slots/{SLOT_ID}/book")

        assert 'data-testid="problem"' in response.text
        assert response.text.strip(), "an empty answer is a blank page"


class TestWhenBookingIsNotThere:
    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param({"status": 500}, id="server-error"),
            pytest.param({"status": 200, "delay_ms": 1500}, id="hangs-longer-than-our-timeout"),
            pytest.param({"fault": "CONNECTION_RESET_BY_PEER"}, id="connection-reset"),
            pytest.param({"status": 200, "json_body": {"unexpected": True}}, id="answers-nonsense"),
        ],
    )
    def test_the_page_says_so_instead_of_breaking(
        self, client: TestClient, booking_stub: WireMock, failure: dict[str, Any]
    ) -> None:
        booking_stub.stub("GET", "/bookings", **failure)

        page = client.get("/bookings")

        assert page.status_code == 200, "a neighbour being down is not our 500"
        assert 'data-testid="page-problem"' in page.text


class TestCancelling:
    def test_the_row_comes_back_cancelled(self, client: TestClient, booking_stub: WireMock) -> None:
        booking_stub.stub(
            "DELETE", f"/bookings/{BOOKING_ID}", json_body=booking_row(state="cancelled")
        )

        response = client.post(f"/bookings/{BOOKING_ID}/cancel")

        assert 'data-state="cancelled"' in response.text
        assert "hx-trigger" not in response.text, "nothing left to wait for"

    def test_cancelling_twice_is_not_an_error(
        self, client: TestClient, booking_stub: WireMock
    ) -> None:
        """Booking answers 200 to a repeat (ADR-0008); a double click must look the same."""
        booking_stub.stub(
            "DELETE", f"/bookings/{BOOKING_ID}", json_body=booking_row(state="cancelled")
        )

        first = client.post(f"/bookings/{BOOKING_ID}/cancel")
        second = client.post(f"/bookings/{BOOKING_ID}/cancel")

        assert first.text == second.text

    def test_someone_elses_booking_is_a_sentence_too(
        self, client: TestClient, booking_stub: WireMock
    ) -> None:
        booking_stub.stub(
            "DELETE", f"/bookings/{BOOKING_ID}", status=404, json_body=error("booking_not_found")
        )

        response = client.post(f"/bookings/{BOOKING_ID}/cancel")

        assert explain("booking_not_found") in response.text


class TestPolling:
    def test_a_pending_booking_keeps_the_trigger(
        self, client: TestClient, booking_stub: WireMock
    ) -> None:
        booking_stub.stub("GET", f"/bookings/{BOOKING_ID}", json_body=booking_row())

        row = client.get(f"/bookings/{BOOKING_ID}/row")

        assert 'hx-trigger="every 1s"' in row.text

    def test_a_confirmed_booking_drops_it(self, client: TestClient, booking_stub: WireMock) -> None:
        booking_stub.stub(
            "GET", f"/bookings/{BOOKING_ID}", json_body=booking_row(state="confirmed")
        )

        row = client.get(f"/bookings/{BOOKING_ID}/row")

        assert "hx-trigger" not in row.text
        assert 'data-state="confirmed"' in row.text


class TestPaying:
    def test_the_person_is_sent_to_the_provider(
        self, client: TestClient, booking_stub: WireMock
    ) -> None:
        booking_stub.stub(
            "POST",
            f"/bookings/{BOOKING_ID}/payment",
            json_body={
                "payment_id": "01999999-0000-7000-8000-000000000003",
                "status": "pending",
                "checkout_url": "https://paystub.example/checkout/ch_1",
            },
        )

        response = client.post(f"/bookings/{BOOKING_ID}/pay")

        assert response.status_code == 303
        assert response.headers["location"] == "https://paystub.example/checkout/ch_1"

    def test_a_payment_without_a_checkout_page_does_not_send_anyone_nowhere(
        self, client: TestClient, booking_stub: WireMock
    ) -> None:
        booking_stub.stub(
            "POST",
            f"/bookings/{BOOKING_ID}/payment",
            json_body={
                "payment_id": "01999999-0000-7000-8000-000000000003",
                "status": "pending",
                "checkout_url": None,
            },
        )

        response = client.post(f"/bookings/{BOOKING_ID}/pay")

        assert response.status_code == 200
        assert 'data-testid="problem"' in response.text

    def test_payments_being_down_is_a_page_not_a_crash(
        self, client: TestClient, booking_stub: WireMock
    ) -> None:
        booking_stub.stub(
            "POST",
            f"/bookings/{BOOKING_ID}/payment",
            status=503,
            json_body=error("payments_unavailable"),
        )

        response = client.post(f"/bookings/{BOOKING_ID}/pay")

        assert explain("payments_unavailable") in response.text


class TestPublishingASlot:
    def test_the_time_on_the_masters_clock_becomes_utc(
        self, master: TestClient, booking_stub: WireMock
    ) -> None:
        """A browser five hours east of UTC: 10:00 there is 05:00 in the API."""
        booking_stub.stub("POST", "/slots", status=201, json_body=slot_row())

        master.post(
            "/my-slots",
            data={
                "starts_at": "2026-09-21T10:00",
                "duration_minutes": "60",
                "price": "1500",
                "timezone_offset": "-300",
            },
        )

        [sent] = booking_stub.received("POST", "/slots")
        body = json.loads(sent["body"])
        assert body["starts_at"].startswith("2026-09-21T05:00:00")
        assert body["ends_at"].startswith("2026-09-21T06:00:00")
        assert body["price_minor"] == 150_000
        assert "master_id" not in body, "who the master is comes from the token"

    def test_a_published_slot_comes_back_as_a_row(
        self, master: TestClient, booking_stub: WireMock
    ) -> None:
        booking_stub.stub("POST", "/slots", status=201, json_body=slot_row())

        response = master.post(
            "/my-slots",
            data=SLOT_FORM,
        )

        assert 'data-testid="my-slot"' in response.text

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param({"price": "no"}, id="price-is-not-a-number"),
            pytest.param({"price": "0"}, id="price-is-zero"),
            pytest.param({"starts_at": "yesterday"}, id="time-is-not-a-time"),
            pytest.param({"timezone_offset": "-5000"}, id="impossible-timezone"),
        ],
    )
    def test_a_form_we_cannot_use_never_reaches_booking(
        self, master: TestClient, booking_stub: WireMock, change: dict[str, str]
    ) -> None:
        booking_stub.stub("POST", "/slots", status=201, json_body=slot_row())
        form = SLOT_FORM | change

        response = master.post("/my-slots", data=form)

        assert 'data-testid="problem"' in response.text
        assert booking_stub.received("POST", "/slots") == []

    def test_a_slot_in_the_past_is_refused_by_booking_and_shown_here(
        self, master: TestClient, booking_stub: WireMock
    ) -> None:
        """The rule lives in booking. The page's job is to say it in words."""
        booking_stub.stub("POST", "/slots", status=422, json_body=error("invalid_slot"))

        response = master.post(
            "/my-slots",
            data=SLOT_FORM | {"starts_at": "2020-01-01T10:00"},
        )

        assert explain("invalid_slot") in response.text

    def test_the_master_sees_their_own_slots(
        self, master: TestClient, booking_stub: WireMock
    ) -> None:
        booking_stub.stub("GET", "/slots", json_body=[slot_row()])

        page = master.get("/my-slots")

        assert 'data-testid="my-slot"' in page.text
        [sent] = booking_stub.received("GET", "/slots")
        assert sent["queryParams"]["master_id"]["values"] == [MASTER_NAME]
