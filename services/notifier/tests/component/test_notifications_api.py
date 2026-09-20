"""The read API: what was this client actually told?

It is the only way to answer that question from outside, so the smoke test
of the whole stack uses it instead of looking into the gateway's stub.
"""

from collections.abc import Callable

from fastapi.testclient import TestClient

from notifier.consumer import Consumer
from notifier.domain import NotificationStatus

from .conftest import MESSAGES, stub_gateway_accepts
from .wiremock import WireMock


def test_notifications_of_a_booking_are_listed(
    client: TestClient, consumer: Consumer, publish: Callable[..., str], notifygw: WireMock
) -> None:
    stub_gateway_accepts(notifygw)
    event_id = publish(booking_id="booking-1", client_id="client-1")
    publish(booking_id="booking-2")
    consumer.run_once()

    response = client.get("/notifications", params={"booking_id": "booking-1"})

    assert response.status_code == 200
    [notification] = response.json()
    assert notification["event_id"] == event_id
    assert notification["client_id"] == "client-1"
    assert notification["status"] == NotificationStatus.SENT
    assert notification["text"] == "Your booking on 2026-09-21 10:30 is confirmed."


def test_a_booking_nobody_was_told_about_has_no_notifications(client: TestClient) -> None:
    response = client.get("/notifications", params={"booking_id": "booking-404"})

    assert response.status_code == 200
    assert response.json() == []


def test_a_notification_that_could_not_be_sent_is_visible_too(
    client: TestClient, consumer: Consumer, publish: Callable[..., str], notifygw: WireMock
) -> None:
    """A message stuck in `sending` is a fact worth seeing, not a hidden state."""
    notifygw.stub("POST", MESSAGES, status=503)
    publish(booking_id="booking-3")
    consumer.run_once()

    [notification] = client.get("/notifications", params={"booking_id": "booking-3"}).json()

    assert notification["status"] == NotificationStatus.SENDING
    assert notification["attempts"] == 1


def test_a_booking_id_outside_the_safe_alphabet_is_refused(client: TestClient) -> None:
    response = client.get("/notifications", params={"booking_id": "booking 1;"})

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
