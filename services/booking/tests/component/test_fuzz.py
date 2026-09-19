"""Fuzzing by the service's own OpenAPI schema (ADR-0007).

Schemathesis generates valid and invalid requests for every operation and
checks that the service never answers 5xx and that every answer matches
what the schema promises. Neighbours answer successfully here, so a 5xx
can only come from this service itself.
"""

from typing import Any

import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, settings
from schemathesis.specs.openapi.checks import positive_data_acceptance

from .wiremock import WireMock


@pytest.fixture
def api_schema(app: FastAPI, happy_neighbours: None) -> Any:
    return schemathesis.openapi.from_asgi("/openapi.json", app)


schema = schemathesis.pytest.from_fixture("api_schema")


@schema.parametrize()
# Noise from the ASGI transport of Schemathesis, not our code; ignored only here.
@pytest.mark.filterwarnings(
    "ignore:Exception ignored while calling deallocator <function MemoryObject"
    ":pytest.PytestUnraisableExceptionWarning"
)
@settings(
    max_examples=40,
    # Same examples on every run: a PR gate must not be a lottery. Randomised,
    # longer runs belong to a nightly job (roadmap: "Долги и находки").
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
def test_api_keeps_the_promises_of_its_schema(case: Any) -> None:
    # A schema cannot express business rules ("a slot starts in the future", "only a
    # pending booking can be paid"), so a well-formed request may be refused with
    # 409/422 on purpose. Every other check stays on: no 5xx, documented status codes,
    # bodies and headers matching the schema.
    case.call_and_validate(excluded_checks=[positive_data_acceptance])


@pytest.fixture
def happy_neighbours(payments_stub: WireMock) -> None:
    payments_stub.stub(
        "POST",
        "/payments",
        status=201,
        json_body={
            "id": "00000000-0000-7000-8000-000000000001",
            "status": "pending",
            "checkout_url": "https://pay/1",
        },
    )
