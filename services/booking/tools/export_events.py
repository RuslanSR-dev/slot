"""Print the JSON Schema of the booking event: the published contract of the stream.

ADR-0010: the schema is generated from code, committed as
contracts/events/booking.v1.json and compared with main to block breaking
changes. The consumer (notifier) declares what it reads from it.
"""

import json
import sys

from booking.schemas import BookingEventV1


def main() -> None:
    schema = BookingEventV1.model_json_schema()
    json.dump(schema, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
