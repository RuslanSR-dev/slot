"""Fuzzing by the service's own OpenAPI schema (ADR-0007).

Schemathesis generates valid and invalid requests for every operation and
checks that the service never answers 5xx and that every answer matches
what the schema promises. The notifier's API is read-only and has no
neighbours, so a 5xx can only come from this service itself.
"""

from typing import Any

import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, settings
from schemathesis.specs.openapi.checks import positive_data_acceptance


@pytest.fixture
def api_schema(app: FastAPI) -> Any:
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
    # Every check stays on: no 5xx, documented status codes, bodies and headers
    # matching the schema. `positive_data_acceptance` is excluded for the same
    # reason as in the other services: a schema cannot express business rules.
    case.call_and_validate(excluded_checks=[positive_data_acceptance])
