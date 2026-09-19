import pytest
from fastapi.testclient import TestClient

from payments import __version__
from payments.app import UNKNOWN_BUILD, Settings, create_app

# Nothing is contacted: /health touches neither the database nor neighbours.
SETTINGS = Settings(
    database_url="postgresql+psycopg://unused:unused@localhost:1/unused",
    paystub_url="http://paystub.invalid",
    paystub_api_key="unused",
    webhook_secret="unused",
    booking_url="http://booking.invalid",
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(SETTINGS))


def test_health_reports_service_and_version(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLOT_BUILD_SHA", "abc1234")

    assert client.get("/health").json() == {
        "status": "ok",
        "service": "payments",
        "version": __version__,
        "build_sha": "abc1234",
    }


@pytest.mark.parametrize("value", [None, ""], ids=["variable-missing", "variable-empty"])
def test_health_reports_unknown_build_when_sha_was_not_provided(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("SLOT_BUILD_SHA", raising=False)
    else:
        monkeypatch.setenv("SLOT_BUILD_SHA", value)

    assert client.get("/health").json()["build_sha"] == UNKNOWN_BUILD


def test_service_does_not_start_without_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """A payments service that accepts unsigned webhooks must not exist."""
    for name, value in {
        "SLOT_DATABASE_URL": SETTINGS.database_url,
        "SLOT_PAYSTUB_URL": SETTINGS.paystub_url,
        "SLOT_PAYSTUB_API_KEY": "key",
        "SLOT_BOOKING_URL": SETTINGS.booking_url,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SLOT_PAYSTUB_WEBHOOK_SECRET", raising=False)

    with pytest.raises(KeyError, match="SLOT_PAYSTUB_WEBHOOK_SECRET"):
        create_app()
