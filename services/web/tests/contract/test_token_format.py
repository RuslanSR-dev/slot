"""The token format, written down from the side that produces it.

Web signs tokens, booking reads them, and the two services share no code
(ADR-0002). So this works like a pact: the producer regenerates the
recorded vectors from its own code, and `make test-contract` fails if the
file changed and was not committed. The consumer - booking - has a unit
test that reads the same file and must understand every token in it.

Change the format on one side only, and one of the two tests goes red.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from web.domain import SESSION_LIFETIME, Role, issue_token

VECTOR_FILE = Path("contracts") / "auth" / "token.v1.json"
FORMAT = "v1.<base64url(claims)>.<base64url(hmac-sha256 of the two parts before it)>"
SECRET = "golden-secret-for-the-vectors"
# Fixed expiry times, so the file is the same on every run. They are far in
# the future: booking's tests verify these tokens against a fixed clock.
VECTORS = [
    {"subject": "anna", "role": Role.MASTER, "expires_at": 1790000000},
    {"subject": "client-42", "role": Role.CLIENT, "expires_at": 1790000000},
    {"subject": "o.brien:1@slot", "role": Role.CLIENT, "expires_at": 2000000000},
]


def repository() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / VECTOR_FILE).exists():
            return parent
    raise AssertionError(f"{VECTOR_FILE} not found above {__file__}")


def record(vector: dict[str, Any]) -> dict[str, Any]:
    # issue_token decides the expiry itself, so the recorded moment is
    # counted back from the expiry we want in the file.
    login = datetime.fromtimestamp(vector["expires_at"], tz=UTC) - SESSION_LIFETIME
    return {
        "subject": vector["subject"],
        "role": str(vector["role"]),
        "expires_at": vector["expires_at"],
        "token": issue_token(vector["subject"], vector["role"], SECRET, login),
    }


def test_the_recorded_tokens_are_the_ones_we_produce_today() -> None:
    """Rewrites the file. A changed format shows up as a changed contract."""
    document = {
        "format": FORMAT,
        "secret": SECRET,
        "vectors": [record(vector) for vector in VECTORS],
    }

    path = repository() / VECTOR_FILE
    path.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    assert json.loads(path.read_text())["vectors"][0]["token"].startswith("v1.")
