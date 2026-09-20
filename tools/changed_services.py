"""Which services a change can break: test impact analysis for CI.

    python tools/changed_services.py BASE_REF   ->  ["payments"]

A service is affected when its own files change, or when a contract it has
to honour changes (a pact where it is the provider, the schema of an
external API it calls, or the schema of an event it publishes or reads).
Files that cannot break a service (docs) affect nothing. Anything else, and
any path this script does not know, affects every service: a wrong "nothing
to run" is far worse than an extra run.
"""

import json
import subprocess
import sys
from collections.abc import Iterable

SERVICES = ("booking", "notifier", "payments", "web")
# External APIs and the services that call them.
EXTERNAL_API_USERS = {"contracts/paystub/": ("payments",), "contracts/notifygw/": ("notifier",)}
# The event schema and the consumer expectations: both sides are re-checked,
# because a change there can break either the producer or the consumer.
EVENT_PARTIES = ("booking", "notifier")
# The session token: web signs it, booking verifies it. A changed format
# breaks whichever side did not change with it.
TOKEN_PARTIES = ("booking", "web")
# Cannot change the behaviour of any service. The system-level test projects
# (smoke, e2e) are here because they run on every change anyway.
IRRELEVANT = ("docs/", "README.md", "CLAUDE.md", ".gitignore", "smoke/", "e2e/")


def affected_services(paths: Iterable[str]) -> list[str]:
    affected: set[str] = set()
    for path in paths:
        if path.startswith(IRRELEVANT):
            continue
        parts = path.split("/")
        if parts[0] == "services" and len(parts) > 2 and parts[1] in SERVICES:
            affected.add(parts[1])
        elif path.startswith("contracts/events/"):
            affected.update(EVENT_PARTIES)
        elif path.startswith("contracts/auth/"):
            affected.update(TOKEN_PARTIES)
        elif path.startswith("contracts/pacts/") and path.endswith(".json"):
            # {consumer}-{provider}.json: the provider must verify the new contract.
            provider = parts[-1].removesuffix(".json").split("-")[-1]
            affected.update([provider] if provider in SERVICES else SERVICES)
        elif any(path.startswith(api) for api in EXTERNAL_API_USERS):
            for api, users in EXTERNAL_API_USERS.items():
                if path.startswith(api):
                    affected.update(users)
        else:
            return sorted(SERVICES)
    return sorted(affected)


def changed_files(base_ref: str) -> list[str]:
    diff = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode != 0:
        # Unknown base (first push, shallow clone): be safe and run everything.
        return ["<unknown base>"]
    return diff.stdout.split()


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: changed_services.py BASE_REF")
    print(json.dumps(affected_services(changed_files(sys.argv[1]))))


if __name__ == "__main__":
    main()
