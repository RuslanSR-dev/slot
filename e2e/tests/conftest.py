"""The browser level. Everything here is set up through the API on purpose.

Rules this project follows (docs/test-strategy.md):

- data is prepared through the booking API, never by clicking through the
  interface: a test that books a slot in order to cancel one fails twice
  for the same defect and takes twice as long;
- the browser runs in a timezone that is not UTC. In UTC a missing
  conversion looks exactly like a correct one;
- the tests know URLs and the webhook secret, nothing about the code. The
  same file could be pointed at a deployed environment.
"""

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from playwright.sync_api import BrowserContext, Page

WEB_URL = os.environ.get("SLOT_WEB_URL", "http://127.0.0.1:8003")
BOOKING_URL = os.environ.get("SLOT_BOOKING_URL", "http://127.0.0.1:8000")
PAYMENTS_URL = os.environ.get("SLOT_PAYMENTS_URL", "http://127.0.0.1:8001")
WEBHOOK_SECRET = os.environ.get("SLOT_PAYSTUB_WEBHOOK_SECRET", "local-webhook-secret")
SESSION_COOKIE = "slot_session"
NAME_COOKIE = "slot_name"
ROLE_COOKIE = "slot_role"
# Five hours east of UTC: in UTC every test below would also pass with the
# conversion removed, which is the whole reason this is not UTC.
TIMEZONE = "Asia/Yekaterinburg"
# The hosted checkout page of PayStub. We do not own it, and in the stack
# the provider is a WireMock that answers with this address.
CHECKOUT_HOST = "https://paystub.example"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    return browser_context_args | {"timezone_id": TIMEZONE, "locale": "ru-RU"}


@pytest.fixture
def api() -> Iterator[httpx2.Client]:
    """The booking API, used only to prepare data and to check the result."""
    with httpx2.Client(base_url=BOOKING_URL, timeout=10) as http:
        yield http


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def sign_in(name: str, role: str) -> str:
    """A session token, obtained the way a person gets one: by logging in."""
    response = httpx2.post(
        f"{WEB_URL}/login",
        data={"name": name, "role": role},
        follow_redirects=False,
        timeout=10,
    )
    assert response.status_code == 303, response.text
    token = response.cookies.get(SESSION_COOKIE)
    assert token, "logging in must set a session"
    return str(token)


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def open_as(context: BrowserContext, name: str, role: str) -> None:
    """Put a session straight into the browser.

    Logging in through the form is a scenario of its own; everywhere else it
    is set-up, and set-up does not go through the interface.
    """
    token = sign_in(name, role)
    context.add_cookies(
        [
            {"name": SESSION_COOKIE, "value": token, "url": WEB_URL},
            {"name": NAME_COOKIE, "value": name, "url": WEB_URL},
            {"name": ROLE_COOKIE, "value": role, "url": WEB_URL},
        ]
    )


def publish_slot(
    api: httpx2.Client, master_token: str, hours_from_now: int = 24, price_minor: int = 150_000
) -> dict[str, Any]:
    starts_at = datetime.now(UTC) + timedelta(hours=hours_from_now)
    response = api.post(
        "/slots",
        json={
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
            "price_minor": price_minor,
        },
        headers=headers(master_token),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def book_slot(api: httpx2.Client, client_token: str, slot_id: str) -> dict[str, Any]:
    response = api.post("/bookings", json={"slot_id": slot_id}, headers=headers(client_token))
    assert response.status_code == 201, response.text
    return dict(response.json())


def start_payment(api: httpx2.Client, client_token: str, booking_id: str) -> dict[str, Any]:
    """Creating a payment is idempotent (ADR-0008), so asking again after the
    browser did it returns the same payment - and with it the reference the
    provider will quote in its webhook."""
    response = api.post(f"/bookings/{booking_id}/payment", headers=headers(client_token))
    assert response.status_code == 200, response.text
    return dict(response.json())


def report_payment(payment_id: str) -> None:
    """Play PayStub: tell payments that the customer paid, with a real signature."""
    event = {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "charge.succeeded",
        "data": {"charge_id": "ch_stack", "reference": payment_id},
    }
    body = json.dumps(event).encode()
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = httpx2.post(
        f"{PAYMENTS_URL}/webhooks/paystub",
        content=body,
        headers={"Content-Type": "application/json", "PayStub-Signature": signature},
        timeout=10,
    )
    assert response.json()["outcome"] == "processed", response.text


def paid_at_the_provider(page: Page) -> str:
    """The address the browser was handed over to.

    The hosted checkout page belongs to PayStub, and PayStub does not exist:
    in the stack it is a WireMock that only answers the API. So what is
    checked here is the hand-over itself - the browser really left our site
    for the provider's address - and the test then plays the provider, the
    same way smoke signs its webhooks. A fake checkout page in the stack is
    written down as a debt in docs/roadmap.md.
    """
    with page.expect_request(f"{CHECKOUT_HOST}/checkout/**") as handed_over:
        page.get_by_test_id("pay").click()
    return str(handed_over.value.url)
