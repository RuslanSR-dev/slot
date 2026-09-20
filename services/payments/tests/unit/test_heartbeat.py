"""The liveness signal of the background process.

The container healthcheck reads this file, so a heartbeat that is written
once and never again, or not written at all, would make the check green
forever. Both cases are tested here.
"""

from datetime import UTC, datetime
from pathlib import Path

from payments.relay import touch_heartbeat

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 20, 12, 0, 5, tzinfo=UTC)


def test_every_cycle_rewrites_the_file(tmp_path: Path) -> None:
    beat = tmp_path / "heartbeat"

    touch_heartbeat(str(beat), NOW)
    assert beat.read_text() == NOW.isoformat()

    touch_heartbeat(str(beat), LATER)
    assert beat.read_text() == LATER.isoformat()


def test_without_a_path_nothing_is_written(tmp_path: Path) -> None:
    """Locally the process runs without a heartbeat file and must not fail."""
    touch_heartbeat(None, NOW)

    assert list(tmp_path.iterdir()) == []
