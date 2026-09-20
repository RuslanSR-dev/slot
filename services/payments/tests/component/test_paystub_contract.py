"""Our PayStub fakes must look like the real PayStub (ADR-0007).

We cannot run PayStub's tests or verify a pact against it: all we have is
its published schema, contracts/paystub/openapi.yaml. So we check the other
way round: everything we send to PayStub, and every fake answer we let our
tests and our stack believe, must match that schema. Otherwise the tests
are green against an API that does not exist.
"""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from payments.paystub import charge_request, refund_request

from .conftest import charge_created, paystub_event, refund_created

REPO = Path(__file__).resolve().parents[4]
SPEC = yaml.safe_load((REPO / "contracts" / "paystub" / "openapi.yaml").read_text())
REGISTRY = Registry().with_resource(
    "urn:paystub", Resource.from_contents(SPEC, default_specification=DRAFT202012)
)
STACK_STUBS = sorted((REPO / "infra" / "paystub" / "mappings").glob("*.json"))


def validate(instance: Any, schema_name: str) -> None:
    Draft202012Validator(
        {"$ref": f"urn:paystub#/components/schemas/{schema_name}"},
        registry=REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(instance)


def test_our_charge_request_matches_paystub_schema() -> None:
    validate(charge_request(uuid.uuid4(), 150_000, "RUB"), "ChargeRequest")


def test_schema_check_catches_a_request_paystub_would_reject() -> None:
    """The check itself must be able to fail: ChargeRequest forbids unknown fields."""
    with pytest.raises(ValidationError):
        validate(
            charge_request(uuid.uuid4(), 150_000, "RUB") | {"amount_rub": 1500}, "ChargeRequest"
        )


# What each stubbed endpoint of the stack must answer with.
RESPONSE_SCHEMAS = {"/v1/charges": "Charge", "/v1/refunds": "Refund"}


@pytest.mark.parametrize("mapping", STACK_STUBS, ids=lambda path: path.name)
def test_stack_stub_answers_like_paystub(mapping: Path) -> None:
    stub = json.loads(mapping.read_text())
    path = stub["request"]["urlPath"]

    assert path in RESPONSE_SCHEMAS, "a stub of an endpoint PayStub does not have"
    validate(stub["response"]["jsonBody"], RESPONSE_SCHEMAS[path])


def test_charge_our_component_tests_fake_matches_paystub_schema() -> None:
    validate(charge_created(), "Charge")


def test_our_refund_request_matches_paystub_schema() -> None:
    validate(refund_request("ch_1", 150_000), "RefundRequest")


def test_schema_check_catches_a_refund_paystub_would_reject() -> None:
    """A refund of a charge id that is not one: the pattern must catch it."""
    with pytest.raises(ValidationError):
        validate(refund_request("not-a-charge", 150_000), "RefundRequest")


def test_refund_our_component_tests_fake_matches_paystub_schema() -> None:
    validate(refund_created(), "Refund")


def test_events_our_tests_send_look_like_paystub_events() -> None:
    validate(paystub_event(str(uuid.uuid4())), "Event")
    validate(paystub_event(str(uuid.uuid4()), "charge.failed"), "Event")
