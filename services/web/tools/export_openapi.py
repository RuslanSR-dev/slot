"""Print the OpenAPI schema of the service: the committed copy is its public contract.

Web answers with HTML, not JSON, so the schema is mostly a list of routes.
It is still generated and committed like everywhere else (ADR-0007): a page
that quietly changed its address or its form fields shows up in the diff.
"""

import json
import sys

from web.app import Settings, create_app

# The schema does not depend on settings; nothing here is contacted or signed.
UNUSED = "unused"
SETTINGS = Settings(booking_url="http://localhost:1", auth_secret=UNUSED)


def main() -> None:
    schema = create_app(SETTINGS).openapi()
    json.dump(schema, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
