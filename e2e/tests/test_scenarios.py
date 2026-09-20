"""Four scenarios. Each one is here because no level below can hold it.

What is deliberately NOT checked through the interface: the rules of who
may touch what (component tests of booking), every refusal of the API
(component tests of web), the double-booking race, expiry, webhooks,
notifications and migrations. A rule proven by a hidden button is not
proven at all.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
from playwright.sync_api import BrowserContext, Page, Request, expect

from .conftest import (
    CHECKOUT_HOST,
    TIMEZONE,
    WEB_URL,
    book_slot,
    headers,
    open_as,
    paid_at_the_provider,
    publish_slot,
    report_payment,
    sign_in,
    start_payment,
    unique,
)

# The confirmation travels from the webhook through an outbox and a relay,
# so the page learns about it a moment later (ADR-0010).
EVENTUALLY = 20_000


def only_booking(api: httpx2.Client, token: str) -> dict[str, Any]:
    bookings = api.get("/bookings", headers=headers(token)).json()
    assert len(bookings) == 1, bookings
    return dict(bookings[0])


def test_a_paid_booking_confirms_itself_while_the_page_is_open(
    page: Page, context: BrowserContext, api: httpx2.Client
) -> None:
    """Book, pay at the provider, come back, and watch the status change.

    Only a browser can show this: the page has to start asking, the person
    has to leave for a site we do not own and come back, and the answer has
    to arrive without anyone pressing anything.
    """
    master, client = unique("master"), unique("client")
    master_token, client_token = sign_in(master, "master"), sign_in(client, "client")
    publish_slot(api, master_token)
    open_as(context, client, "client")

    page.goto(f"{WEB_URL}/masters/{master}")
    page.get_by_test_id("book").click()
    expect(page.get_by_test_id("booked")).to_be_visible()

    page.goto(f"{WEB_URL}/bookings")
    expect(page.get_by_test_id("state-text")).to_have_text("Ждём оплату")
    # The page starts asking about this booking on its own.
    expect(page.locator("[hx-trigger]")).to_have_count(1)

    assert paid_at_the_provider(page).startswith(f"{CHECKOUT_HOST}/checkout/")

    # Back from the provider, where our polling has to pick up again. The
    # customer returns by a fresh navigation, the way a provider's return URL
    # brings them back - not with the back button. That also keeps the test
    # away from a race it cannot win: `paystub.example` resolves nowhere, the
    # browser ends up on its own error page, and going back from a navigation
    # that is still settling failed about once in ten runs with "Not attached
    # to an active page".
    page.goto(f"{WEB_URL}/bookings")
    expect(page.get_by_test_id("state-text")).to_have_text("Ждём оплату")

    # The customer paid. Nobody touches the browser after this line.
    payment = start_payment(api, client_token, only_booking(api, client_token)["id"])
    report_payment(payment["payment_id"])

    expect(page.get_by_test_id("state-text")).to_have_text("Оплачено", timeout=EVENTUALLY)
    # ...and stops as soon as there is nothing left to wait for.
    expect(page.locator("[hx-trigger]")).to_have_count(0)


def test_cancelling_a_booking_frees_the_slot_for_someone_else(
    page: Page, context: BrowserContext, api: httpx2.Client
) -> None:
    """Two pages and one impatient person.

    The state changes on the bookings page, and the result has to be visible
    on another page. The double click is here because a button that sends
    the request twice is invisible at every level below.
    """
    master, client = unique("master"), unique("client")
    master_token, client_token = sign_in(master, "master"), sign_in(client, "client")
    slot = publish_slot(api, master_token)
    book_slot(api, client_token, slot["id"])
    open_as(context, client, "client")

    cancels: list[str] = []

    def remember(request: Request) -> None:
        if request.method == "POST" and request.url.endswith("/cancel"):
            cancels.append(request.url)

    page.on("request", remember)

    page.goto(f"{WEB_URL}/bookings")
    page.get_by_test_id("cancel").dblclick()

    expect(page.get_by_test_id("state-text")).to_have_text("Отменена")
    expect(page.get_by_test_id("cancel")).to_have_count(0)
    assert len(cancels) == 1, f"one click, one request; got {cancels}"

    page.goto(f"{WEB_URL}/masters/{master}")

    expect(page.get_by_test_id("book")).to_be_visible()


def test_a_slot_taken_while_the_client_was_looking_at_it(
    page: Page, context: BrowserContext, api: httpx2.Client
) -> None:
    """The race of ADR-0005 as a person meets it.

    The conflict is proven on the component level; what only a browser can
    show is what is left on the screen afterwards - a sentence, not an error
    code, and not a blank page.
    """
    master, client, faster = unique("master"), unique("client"), unique("faster")
    master_token = sign_in(master, "master")
    slot = publish_slot(api, master_token)
    open_as(context, client, "client")

    page.goto(f"{WEB_URL}/masters/{master}")
    expect(page.get_by_test_id("book")).to_be_visible()

    # While the page was open, somebody else took the slot.
    book_slot(api, sign_in(faster, "client"), slot["id"])

    page.get_by_test_id("book").click()

    expect(page.get_by_test_id("problem")).to_have_text(
        "Этот слот только что заняли. Выберите другое время."
    )
    expect(page.get_by_role("heading", name=f"Слоты мастера {master}")).to_be_visible()
    assert "slot_already_booked" not in page.content(), "an error code is not an answer"


def test_a_master_publishes_a_slot_at_the_time_they_see(
    page: Page, context: BrowserContext, api: httpx2.Client
) -> None:
    """The browser is five hours east of UTC, and that is the point.

    The form sends wall-clock time, the API stores UTC. In a UTC browser
    this test would pass with the conversion deleted.
    """
    master = unique("master")
    open_as(context, master, "master")
    local = (datetime.now(ZoneInfo(TIMEZONE)) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )

    page.goto(f"{WEB_URL}/my-slots")
    page.get_by_test_id("starts-at").fill(local.strftime("%Y-%m-%dT%H:%M"))
    page.get_by_test_id("price").fill("1500")
    page.get_by_test_id("publish").click()

    expect(page.get_by_test_id("my-slot")).to_have_count(1)
    expect(page.get_by_test_id("my-slot")).to_contain_text("10:00")
    expect(page.get_by_test_id("my-slot")).to_contain_text("1500.00")

    [stored] = api.get("/slots", params={"master_id": master}).json()
    assert datetime.fromisoformat(stored["starts_at"]) == local.astimezone(UTC)
    assert stored["price_minor"] == 150_000, "roubles on the page, kopecks in the API"
