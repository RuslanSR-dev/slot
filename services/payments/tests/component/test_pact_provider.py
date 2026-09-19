"""payments keeps the promises its consumers wrote down (ADR-0007).

Every pact file whose provider is payments is replayed against the real
service with its real database. Files come from SLOT_PACT_DIRS: the branch
and the base branch (main). The second catches a change that the consumer
deployed today would not survive, even if the same PR updated its pact.
"""

import os
import uuid
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pact import Verifier
from sqlalchemy import Engine, text

from .conftest import CHARGES, create_payment, stub_charge_created
from .wiremock import WireMock

PROVIDER = "payments"
REPO = Path(__file__).resolve().parents[4]


def pact_files() -> list[Any]:
    dirs = os.environ.get("SLOT_PACT_DIRS") or str(REPO / "contracts" / "pacts")
    found = [
        pytest.param(path, id=f"{Path(directory).name}/{path.name}")
        for directory in dirs.split(os.pathsep)
        for path in sorted(Path(directory).glob(f"*-{PROVIDER}.json"))
    ]
    # An empty list would silently skip the gate.
    assert found, f"no pact files for {PROVIDER} in {dirs}"
    return found


# pact-python's state-handler server calls shutdown() but never server_close()
# (pact/_server.py), leaking its listening socket. Ignored only in this test.
@pytest.mark.filterwarnings(
    "ignore:Exception ignored while finalizing socket:pytest.PytestUnraisableExceptionWarning"
)
@pytest.mark.parametrize("pact_file", pact_files())
def test_payments_honours_its_consumers(
    pact_file: Path, live_server: str, stubs: WireMock, engine: Engine
) -> None:
    def given(state: str, action: str, parameters: dict[str, Any] | None) -> None:
        if action == "teardown":
            stubs.reset()
            with engine.begin() as connection:
                connection.execute(text("TRUNCATE payments, provider_events"))
            return
        params = parameters or {}
        if state == "the payment provider accepts charges":
            stub_charge_created(stubs)
        elif state == "a payment exists for the booking":
            stub_charge_created(stubs)
            with httpx2.Client(base_url=live_server) as http:
                booking_id = uuid.UUID(params["booking_id"])
                create_payment(http, booking_id, params["amount_minor"]).raise_for_status()
        elif state == "the payment provider is down":
            stubs.stub("POST", CHARGES, status=500)
        else:
            raise ValueError(f"payments does not know the provider state {state!r}")

    (
        Verifier(PROVIDER, host="127.0.0.1")
        .add_transport(url=live_server)
        .add_source(pact_file)
        .state_handler(given, teardown=True)
        .verify()
    )
