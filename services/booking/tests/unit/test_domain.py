from datetime import UTC, datetime, timedelta

import pytest

from booking.domain import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    BookingStatus,
    InvalidSlotError,
    InvalidTransitionError,
    SlotInPastError,
    ensure_bookable,
    ensure_transition,
    ensure_valid_slot,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


class TestSlotValidation:
    def test_future_slot_with_positive_duration_is_valid(self) -> None:
        ensure_valid_slot(NOW + HOUR, NOW + 2 * HOUR, now=NOW)

    @pytest.mark.parametrize(
        ("starts_at", "ends_at"),
        [
            pytest.param(NOW + HOUR, NOW + HOUR, id="zero-duration"),
            pytest.param(NOW + 2 * HOUR, NOW + HOUR, id="ends-before-start"),
            pytest.param(NOW - HOUR, NOW + HOUR, id="starts-in-past"),
            pytest.param(NOW, NOW + HOUR, id="starts-exactly-now"),
            pytest.param(
                (NOW + HOUR).replace(tzinfo=None), NOW + 2 * HOUR, id="start-without-timezone"
            ),
            pytest.param(
                NOW + HOUR, (NOW + 2 * HOUR).replace(tzinfo=None), id="end-without-timezone"
            ),
        ],
    )
    def test_invalid_slot_is_rejected(self, starts_at: datetime, ends_at: datetime) -> None:
        with pytest.raises(InvalidSlotError):
            ensure_valid_slot(starts_at, ends_at, now=NOW)


class TestBookable:
    def test_future_slot_is_bookable(self) -> None:
        ensure_bookable(NOW + timedelta(seconds=1), now=NOW)

    @pytest.mark.parametrize(
        "starts_at",
        [
            pytest.param(NOW, id="starts-exactly-now"),
            pytest.param(NOW - timedelta(seconds=1), id="already-started"),
        ],
    )
    def test_started_slot_is_not_bookable(self, starts_at: datetime) -> None:
        with pytest.raises(SlotInPastError):
            ensure_bookable(starts_at, now=NOW)


class TestTransitions:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (BookingStatus.PENDING, BookingStatus.CONFIRMED),
            (BookingStatus.PENDING, BookingStatus.CANCELLED),
            (BookingStatus.PENDING, BookingStatus.EXPIRED),
            (BookingStatus.CONFIRMED, BookingStatus.CANCELLED),
        ],
    )
    def test_allowed_transition(self, current: BookingStatus, target: BookingStatus) -> None:
        ensure_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (current, target)
            for current in BookingStatus
            for target in BookingStatus
            if target not in ALLOWED_TRANSITIONS[current]
        ],
    )
    def test_every_other_transition_is_rejected(
        self, current: BookingStatus, target: BookingStatus
    ) -> None:
        with pytest.raises(InvalidTransitionError):
            ensure_transition(current, target)

    def test_terminal_statuses_have_no_way_out(self) -> None:
        assert ALLOWED_TRANSITIONS[BookingStatus.CANCELLED] == frozenset()
        assert ALLOWED_TRANSITIONS[BookingStatus.EXPIRED] == frozenset()


def test_only_pending_and_confirmed_occupy_a_slot() -> None:
    assert frozenset({BookingStatus.PENDING, BookingStatus.CONFIRMED}) == ACTIVE_STATUSES
