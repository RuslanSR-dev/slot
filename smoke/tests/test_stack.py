"""Smoke tests of the running stack (`make up`).

Black box: they know URLs and the webhook secret, nothing about the code.
The same file will later check a deployed environment, so every test makes
its own unique data and never relies on an empty database.
"""

import hashlib
import hmac
import json
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest

BOOKING_URL = os.environ.get("SLOT_BOOKING_URL", "http://127.0.0.1:8000")
PAYMENTS_URL = os.environ.get("SLOT_PAYMENTS_URL", "http://127.0.0.1:8001")
NOTIFIER_URL = os.environ.get("SLOT_NOTIFIER_URL", "http://127.0.0.1:8002")
# The notification travels through an outbox, a broker and a worker, so it
# arrives a moment later than the answer to the request (ADR-0010).
EVENTUALLY = 20.0
WEBHOOK_SECRET = os.environ.get("SLOT_PAYSTUB_WEBHOOK_SECRET", "local-webhook-secret")
# Empty counts as missing: `make` passes an empty value when git is unavailable.
EXPECTED_BUILD_SHA = os.environ.get("SLOT_EXPECTED_BUILD_SHA") or None
# GitHub Actions and most CI systems set CI=true.
IN_CI = os.environ.get("CI") == "true"
SERVICES = {"booking": BOOKING_URL, "payments": PAYMENTS_URL, "notifier": NOTIFIER_URL}


def wait_until(condition: Callable[[], Any], timeout: float, what: str) -> Any:
    """Poll a condition instead of sleeping a fixed time (see docs/test-strategy.md).

    Returns what the condition returned, so the test can assert on it.
    """
    deadline = time.monotonic() + timeout
    while not (result := condition()):
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not happen within {timeout}s")
        time.sleep(0.1)
    return result


@pytest.fixture
def booking() -> Any:
    with httpx2.Client(base_url=BOOKING_URL, timeout=10) as http:
        yield http


@pytest.fixture
def payments() -> Any:
    with httpx2.Client(base_url=PAYMENTS_URL, timeout=10) as http:
        yield http


@pytest.fixture
def notifier() -> Any:
    with httpx2.Client(base_url=NOTIFIER_URL, timeout=10) as http:
        yield http


@pytest.mark.parametrize("service", SERVICES)
def test_service_is_up(service: str) -> None:
    response = httpx2.get(f"{SERVICES[service]}/health", timeout=5)

    assert response.status_code == 200
    assert response.json()["service"] == service


@pytest.mark.parametrize("service", SERVICES)
def test_running_build_is_the_one_we_just_built(service: str) -> None:
    if EXPECTED_BUILD_SHA is None:
        # A skipped gate looks green. In CI that would silently switch this check off.
        if IN_CI:
            pytest.fail("SLOT_EXPECTED_BUILD_SHA must be set in CI, otherwise this gate is off")
        pytest.skip("SLOT_EXPECTED_BUILD_SHA is not set; allowed only outside CI")

    response = httpx2.get(f"{SERVICES[service]}/health", timeout=5)

    assert response.json()["build_sha"] == EXPECTED_BUILD_SHA


def signed_webhook(payments: httpx2.Client, event: dict[str, Any]) -> httpx2.Response:
    """What PayStub does: HMAC-SHA256 over the exact body bytes."""
    body = json.dumps(event).encode()
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return payments.post(
        "/webhooks/paystub",
        content=body,
        headers={"Content-Type": "application/json", "PayStub-Signature": signature},
    )


def test_booking_is_paid_confirmed_and_the_client_is_told(
    booking: httpx2.Client, payments: httpx2.Client, notifier: httpx2.Client
) -> None:
    """booking -> payments -> PayStub -> webhook -> booking -> outbox -> Redis -> notifier."""
    starts_at = datetime.now(UTC) + timedelta(days=1)
    slot = booking.post(
        "/slots",
        json={
            "master_id": f"smoke-{uuid.uuid4().hex[:12]}",
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
            "price_minor": 150_000,
        },
    )
    assert slot.status_code == 201, slot.text
    created = booking.post(
        "/bookings", json={"slot_id": slot.json()["id"], "client_id": "smoke-client"}
    )
    assert created.status_code == 201, created.text
    booking_id = created.json()["id"]

    payment = booking.post(f"/bookings/{booking_id}/payment")
    assert payment.status_code == 200, payment.text
    assert payment.json()["checkout_url"], "the provider stub must have been reached"

    # The customer paid on the checkout page; PayStub reports it.
    event = {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "charge.succeeded",
        "data": {"charge_id": "ch_stack", "reference": payment.json()["payment_id"]},
    }
    delivered = signed_webhook(payments, event)
    redelivered = signed_webhook(payments, event)

    assert delivered.json() == {"outcome": "processed"}, delivered.text
    assert redelivered.json() == {"outcome": "duplicate"}

    # Since ADR-0010 the webhook only records the event: confirming the booking
    # and telling the client both happen after the answer to PayStub.
    wait_until(
        lambda: booking.get(f"/bookings/{booking_id}").json()["status"] == "confirmed",
        timeout=EVENTUALLY,
        what="the booking to be confirmed",
    )

    # The last step is asynchronous: the event goes through the outbox, the
    # relay and the broker before the notifier sends anything.
    # Waiting for the end state, not for the row to appear: a notification
    # that exists in `sending` proves only that the event arrived.
    sent = wait_until(
        lambda: [
            notification
            for notification in notifier.get(
                "/notifications", params={"booking_id": booking_id}
            ).json()
            if notification["status"] == "sent"
        ],
        timeout=EVENTUALLY,
        what="the notification about the confirmed booking to be sent",
    )

    assert [notification["event_type"] for notification in sent] == ["booking.confirmed"]
    assert sent[0]["client_id"] == "smoke-client"
