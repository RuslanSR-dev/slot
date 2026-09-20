"""Block breaking changes to an event schema.

    python tools/event_schema_diff.py OLD.json NEW.json

Events are not requests: nobody answers "400, you broke me". A consumer that
stops finding a field starts failing on messages that are already in the
stream, and the producer never learns about it. So the schema is committed
and compared with the deployed one, exactly like the OpenAPI schemas
(ADR-0007, ADR-0010).

Breaking for a consumer:

- a property disappeared, or stopped being required: the consumer reads it;
- the type of a property changed;
- a value disappeared from an enum: messages with that value may still be in
  the stream, and a validating consumer would now reject them.

Not breaking, and therefore allowed: a new property, and a new value in an
enum. Consumers must ignore fields and event types they do not know - that
rule is in docs/test-strategy.md, and the notifier has a test for it.
"""

import json
import sys
from pathlib import Path
from typing import Any

# What actually describes the value. Titles and descriptions are documentation.
SHAPE_KEYS = ("type", "format", "const", "enum", "items")


def resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Replace a local $ref with what it points at ($defs of the same file)."""
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return schema
    target: Any = root
    for part in reference.removeprefix("#/").split("/"):
        target = target.get(part, {})
    return target if isinstance(target, dict) else {}


def shape(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    resolved = resolve(schema, root)
    return {key: resolved[key] for key in SHAPE_KEYS if key in resolved}


def breaking_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    old_properties: dict[str, Any] = old.get("properties", {})
    new_properties: dict[str, Any] = new.get("properties", {})
    old_required, new_required = set(old.get("required", [])), set(new.get("required", []))

    for name, old_property in old_properties.items():
        if name not in new_properties:
            problems.append(f"property '{name}' was removed")
            continue
        if name in old_required and name not in new_required:
            problems.append(f"property '{name}' is no longer required")
        was, now = shape(old_property, old), shape(new_properties[name], new)
        for key in SHAPE_KEYS:
            if key == "enum":
                continue
            if was.get(key) != now.get(key):
                problems.append(
                    f"property '{name}': {key} changed from {was.get(key)} to {now.get(key)}"
                )
        removed = [value for value in was.get("enum", []) if value not in now.get("enum", [])]
        if removed:
            problems.append(f"property '{name}': enum values removed: {sorted(removed)}")
    return problems


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: event_schema_diff.py OLD.json NEW.json")
        return 2
    old_path, new_path = Path(sys.argv[1]), Path(sys.argv[2])
    if not old_path.exists():
        # No schema in the base ref: nothing to break yet.
        print(f"--- {new_path}: no schema in the base version, nothing to break")
        return 0
    problems = breaking_changes(json.loads(old_path.read_text()), json.loads(new_path.read_text()))
    if problems:
        print(f"--- {new_path}: breaking changes for consumers")
        for problem in problems:
            print(f"    {problem}")
        print(
            "    a consumer cannot be asked to redeploy first: publish a new version"
            " of the stream instead"
        )
        return 1
    print(f"--- {new_path}: no breaking changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
