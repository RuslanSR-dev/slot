"""The gate that decides whether an event change is breaking is itself a gate:
if it silently returns "fine" for everything, nothing protects the consumers."""

from typing import Any

import pytest

from event_schema_diff import breaking_changes

BASE: dict[str, Any] = {
    "properties": {
        "event_id": {"format": "uuid", "type": "string"},
        "type": {"$ref": "#/$defs/BookingEventType"},
        "version": {"const": 1, "type": "integer"},
        "client_id": {"type": "string"},
    },
    "required": ["event_id", "type", "version", "client_id"],
    "$defs": {
        "BookingEventType": {
            "enum": ["booking.confirmed", "booking.cancelled"],
            "type": "string",
        }
    },
}


def changed(**properties: Any) -> dict[str, Any]:
    new = {**BASE, "properties": {**BASE["properties"], **properties}}
    return new


def test_identical_schema_is_not_a_change() -> None:
    assert breaking_changes(BASE, BASE) == []


def test_removed_property_breaks_consumers() -> None:
    new = {**BASE, "properties": {k: v for k, v in BASE["properties"].items() if k != "client_id"}}

    assert breaking_changes(BASE, new) == ["property 'client_id' was removed"]


def test_property_that_became_optional_breaks_consumers() -> None:
    new = {**BASE, "required": ["event_id", "type", "version"]}

    assert breaking_changes(BASE, new) == ["property 'client_id' is no longer required"]


def test_changed_type_breaks_consumers() -> None:
    [problem] = breaking_changes(BASE, changed(client_id={"type": "integer"}))

    assert "type changed from string to integer" in problem


def test_removed_enum_value_breaks_consumers() -> None:
    """Messages with the removed value can still be in the stream."""
    new = {
        **BASE,
        "$defs": {"BookingEventType": {"enum": ["booking.confirmed"], "type": "string"}},
    }

    [problem] = breaking_changes(BASE, new)
    assert problem == "property 'type': enum values removed: ['booking.cancelled']"


@pytest.mark.parametrize(
    ("what", "new"),
    [
        pytest.param(
            "new property",
            changed(master_id={"type": "string"}),
            id="added-property",
        ),
        pytest.param(
            "new event type",
            {
                **BASE,
                "$defs": {
                    "BookingEventType": {
                        "enum": ["booking.confirmed", "booking.cancelled", "booking.expired"],
                        "type": "string",
                    }
                },
            },
            id="added-enum-value",
        ),
    ],
)
def test_additions_are_allowed(what: str, new: dict[str, Any]) -> None:
    """Consumers must ignore what they do not know, so a %s is not breaking."""
    assert breaking_changes(BASE, new) == [], what
