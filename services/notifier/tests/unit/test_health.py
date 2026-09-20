import pytest
from fastapi.testclient import TestClient

from notifier import __version__
from notifier.app import UNKNOWN_BUILD, Settings, create_app

UNUSED_DATABASE = "postgresql+psycopg://unused:unused@localhost:1/unused"


@pytest.fixture
def client() -> TestClient:
    # The schema and /health do not need a database; nothing connects to this URL.
    return TestClient(create_app(Settings(database_url=UNUSED_DATABASE)))


def test_health_reports_service_and_version(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLOT_BUILD_SHA", "abc1234")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "notifier",
        "version": __version__,
        "build_sha": "abc1234",
    }


@pytest.mark.parametrize(
    "build_sha_env",
    [
        pytest.param(None, id="variable-missing"),
        pytest.param("", id="variable-empty"),
    ],
)
def test_health_reports_unknown_build_when_sha_was_not_provided(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, build_sha_env: str | None
) -> None:
    if build_sha_env is None:
        monkeypatch.delenv("SLOT_BUILD_SHA", raising=False)
    else:
        monkeypatch.setenv("SLOT_BUILD_SHA", build_sha_env)

    assert client.get("/health").json()["build_sha"] == UNKNOWN_BUILD
