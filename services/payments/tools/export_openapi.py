"""Print the OpenAPI schema of the service: the committed copy is its public contract.

ADR-0007: the schema is generated from code, committed as openapi.json, and
compared with main to block breaking changes.
"""

import json
import sys

from payments.app import Settings, create_app

# The schema does not depend on settings; these values are never contacted.
SETTINGS = Settings(
    database_url="postgresql+psycopg://unused:unused@localhost:1/unused",
    paystub_url="http://paystub.invalid",
    paystub_api_key="unused",
    webhook_secret="unused",  # noqa: S106 - never used, the schema does not depend on it
    booking_url="http://booking.invalid",
)


def main() -> None:
    schema = create_app(SETTINGS).openapi()
    json.dump(schema, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
