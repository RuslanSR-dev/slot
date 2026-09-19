"""booking keeps the promises its consumers wrote down (ADR-0007).

Every pact file whose provider is booking is replayed against the real
service with its real database. Files come from SLOT_PACT_DIRS: the branch
and the base branch (main). The second catches a change that the consumer
deployed today would not survive, even if the same PR updated its pact.
"""

import os
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pact import Verifier
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from booking.domain import BookingStatus
from booking.models import Booking, Slot

from .conftest import NOW

PROVIDER = "booking"
REPO = Path(__file__).resolve().parents[4]
STATE_STATUS = {
    "a pending booking exists": BookingStatus.PENDING,
    "a confirmed booking exists": BookingStatus.CONFIRMED,
    "an expired booking exists": BookingStatus.EXPIRED,
}


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


def insert_booking(engine: Engine, booking_id: uuid.UUID, status: BookingStatus) -> None:
    """Put the booking straight into the database: the state is a precondition, not a test."""
    with Session(engine) as session:
        slot = Slot(
            master_id="pact",
            starts_at=NOW + timedelta(hours=1),
            ends_at=NOW + timedelta(hours=2),
            price_minor=150_000,
        )
        session.add(slot)
        session.flush()
        session.add(
            Booking(
                id=booking_id,
                slot_id=slot.id,
                client_id="pact",
                status=status,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()


# pact-python's state-handler server calls shutdown() but never server_close()
# (pact/_server.py), leaking its listening socket. Ignored only in this test.
@pytest.mark.filterwarnings(
    "ignore:Exception ignored while finalizing socket:pytest.PytestUnraisableExceptionWarning"
)
@pytest.mark.parametrize("pact_file", pact_files())
def test_booking_honours_its_consumers(pact_file: Path, live_server: str, engine: Engine) -> None:
    def given(state: str, action: str, parameters: dict[str, Any] | None) -> None:
        if action == "teardown":
            with engine.begin() as connection:
                connection.execute(text("TRUNCATE bookings, slots"))
            return
        if state not in STATE_STATUS:
            raise ValueError(f"booking does not know the provider state {state!r}")
        booking_id = uuid.UUID((parameters or {})["booking_id"])
        insert_booking(engine, booking_id, STATE_STATUS[state])

    (
        Verifier(PROVIDER, host="127.0.0.1")
        .add_transport(url=live_server)
        .add_source(pact_file)
        .state_handler(given, teardown=True)
        .verify()
    )
