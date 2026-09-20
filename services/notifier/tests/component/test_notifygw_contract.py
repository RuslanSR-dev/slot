"""Our NotifyGate fakes must look like the real NotifyGate (ADR-0007).

We cannot run their tests or verify a pact against them: all we have is the
published schema, contracts/notifygw/openapi.yaml. So the check goes the
other way round - everything we send, and every fake answer our tests and
our stack believe, must match that schema.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from notifier.gateway import message_request

from .conftest import gateway_message

REPO = Path(__file__).resolve().parents[4]
SPEC = yaml.safe_load((REPO / "contracts" / "notifygw" / "openapi.yaml").read_text())
REGISTRY = Registry().with_resource(
    "urn:notifygw", Resource.from_contents(SPEC, default_specification=DRAFT202012)
)
STACK_STUBS = sorted((REPO / "infra" / "notifygw" / "mappings").glob("*.json"))


def validate(instance: Any, schema_name: str) -> None:
    Draft202012Validator(
        {"$ref": f"urn:notifygw#/components/schemas/{schema_name}"},
        registry=REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(instance)


def test_our_message_matches_the_gateway_schema() -> None:
    validate(
        message_request("client-1", "Your booking on 2026-09-21 10:30 is confirmed."),
        "MessageRequest",
    )


def test_schema_check_catches_a_message_the_gateway_would_reject() -> None:
    """The check itself must be able to fail: MessageRequest forbids unknown fields."""
    with pytest.raises(ValidationError):
        validate(message_request("client-1", "hi") | {"channel": "sms"}, "MessageRequest")


def test_an_empty_text_is_rejected_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        validate(message_request("client-1", ""), "MessageRequest")


@pytest.mark.parametrize("mapping", STACK_STUBS, ids=lambda path: path.name)
def test_stack_stub_answers_like_the_gateway(mapping: Path) -> None:
    stub = json.loads(mapping.read_text())
    assert stub["request"]["urlPath"] == "/v1/messages"
    validate(stub["response"]["jsonBody"], "Message")


def test_the_answer_our_component_tests_fake_matches_the_gateway_schema() -> None:
    validate(gateway_message(), "Message")
