"""The consumer side of the event contract (ADR-0010).

A Pact test records what a consumer needs from an HTTP provider. Events have
no request and no response, so the same idea takes a different shape: the
notifier writes down which fields it reads and which event types it handles,
and this test checks that the published schema of booking still provides
them.

The file it writes is committed, exactly like a pact file. A change in it is
a change of the contract and has to be seen in review.
"""

import json
from pathlib import Path
from typing import Any

from notifier.consumer import DEFAULT_STREAM
from notifier.domain import MESSAGES, REQUIRED_FIELDS

REPO = Path(__file__).resolve().parents[4]
EVENTS = REPO / "contracts" / "events"
PRODUCER_SCHEMA = EVENTS / "booking.v1.json"
CONTRACT = EVENTS / "consumers" / "notifier.json"


def expectations() -> dict[str, Any]:
    return {
        "consumer": "notifier",
        "producer": "booking",
        "stream": DEFAULT_STREAM,
        "schema": str(PRODUCER_SCHEMA.relative_to(REPO)),
        "required_fields": sorted(REQUIRED_FIELDS),
        "handled_types": sorted(MESSAGES),
    }


def published_types(schema: dict[str, Any]) -> list[str]:
    reference = schema["properties"]["type"]["$ref"].removeprefix("#/")
    target: Any = schema
    for part in reference.split("/"):
        target = target[part]
    types: list[str] = target["enum"]
    return types


def test_the_contract_file_is_written_and_committed() -> None:
    """Written by the test, like a pact file: the Makefile fails on an uncommitted change."""
    CONTRACT.write_text(json.dumps(expectations(), indent=2, sort_keys=True) + "\n")

    assert json.loads(CONTRACT.read_text()) == expectations()


def test_booking_publishes_every_field_the_notifier_reads() -> None:
    schema = json.loads(PRODUCER_SCHEMA.read_text())

    missing = [field for field in REQUIRED_FIELDS if field not in schema["properties"]]
    optional = [field for field in REQUIRED_FIELDS if field not in schema["required"]]

    assert missing == [], "booking no longer publishes a field the notifier needs"
    assert optional == [], "a field the notifier needs became optional"


def test_every_event_the_notifier_handles_is_one_booking_can_publish() -> None:
    """The other direction: a text nobody will ever send is dead code."""
    schema = json.loads(PRODUCER_SCHEMA.read_text())

    unknown = [event_type for event_type in MESSAGES if event_type not in published_types(schema)]

    assert unknown == [], "the notifier renders a text for an event booking never sends"
