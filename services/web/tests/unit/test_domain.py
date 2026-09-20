"""The decisions web makes on its own: the session token and the local clock.

Pure logic, so this is also the module mutmut mutates. The expected token
is written out in full: comparing our own output with our own algorithm
would pass even if the format quietly changed, and that format is what
booking has to read.
"""

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from web.domain import (
    ERROR_MESSAGES,
    MAX_NAME_LENGTH,
    SESSION_LIFETIME,
    UNEXPECTED,
    InvalidInputError,
    Role,
    clean_name,
    explain,
    issue_token,
    parse_role,
    price_to_minor,
    price_to_roubles,
    to_utc,
)

SECRET = "unit-test-secret"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
# Recorded, not recomputed: this is the exact string booking must be able to
# read. It expires 12 hours after NOW, which pins the session lifetime too.
RECORDED_TOKEN = (
    "v1.eyJleHAiOjE3ODk5NDg4MDAsInJvbGUiOiJtYXN0ZXIiLCJzdWIiOiJhbm5hIn0"
    ".krD0SeXju-cSkilQEYSCqaJPYFWg_MloaPvVeicfVUk"
)


class TestSessionToken:
    def test_a_token_is_exactly_the_bytes_booking_expects(self) -> None:
        assert issue_token("anna", Role.MASTER, SECRET, NOW) == RECORDED_TOKEN

    def test_another_secret_gives_another_signature(self) -> None:
        other = issue_token("anna", Role.MASTER, "another-secret", NOW)

        assert other.rsplit(".", 1)[0] == RECORDED_TOKEN.rsplit(".", 1)[0], "same claims"
        assert other != RECORDED_TOKEN, "a secret that does not sign anything is not a secret"

    @pytest.mark.parametrize(
        ("subject", "role"),
        [
            pytest.param("anna", Role.CLIENT, id="a-client"),
            pytest.param("boris", Role.MASTER, id="a-master"),
        ],
    )
    def test_the_claims_are_the_ones_we_were_given(self, subject: str, role: Role) -> None:
        payload = issue_token(subject, role, SECRET, NOW).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "==="))

        assert claims["sub"] == subject
        assert claims["role"] == role
        assert claims["exp"] == int((NOW + SESSION_LIFETIME).timestamp())

    def test_a_later_login_gives_a_later_expiry(self) -> None:
        later = issue_token("anna", Role.MASTER, SECRET, NOW + timedelta(seconds=1))

        assert later != RECORDED_TOKEN

    def test_the_session_is_measured_in_hours_not_minutes(self) -> None:
        """A session that ends in the middle of a booking is a defect of its own."""
        assert timedelta(hours=1) <= SESSION_LIFETIME


class TestNameFromTheLoginForm:
    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            pytest.param("anna", "anna", id="plain"),
            pytest.param("  anna  ", "anna", id="spaces-around"),
            pytest.param("o.brien:1@slot-2", "o.brien:1@slot-2", id="the-whole-alphabet"),
            pytest.param("a" * MAX_NAME_LENGTH, "a" * MAX_NAME_LENGTH, id="longest-allowed"),
        ],
    )
    def test_a_name_we_can_use(self, typed: str, expected: str) -> None:
        assert clean_name(typed) == expected

    @pytest.mark.parametrize(
        "typed",
        [
            pytest.param("", id="empty"),
            pytest.param("   ", id="only-spaces"),
            pytest.param("anna smith", id="space-inside"),
            pytest.param("anna\x00", id="nul-byte"),
            pytest.param("анна", id="not-in-the-alphabet"),
            pytest.param("a" * (MAX_NAME_LENGTH + 1), id="one-character-too-long"),
        ],
    )
    def test_a_name_we_refuse_before_anyone_is_called(self, typed: str) -> None:
        with pytest.raises(InvalidInputError):
            clean_name(typed)

    @pytest.mark.parametrize("value", ["client", "master"])
    def test_the_two_roles_we_know(self, value: str) -> None:
        assert parse_role(value) == Role(value)

    @pytest.mark.parametrize("value", ["admin", "", "CLIENT"])
    def test_any_other_role_is_refused(self, value: str) -> None:
        with pytest.raises(InvalidInputError):
            parse_role(value)


class TestLocalTime:
    """A test that runs in UTC cannot tell a conversion from a missing one."""

    @pytest.mark.parametrize(
        ("offset", "expected_hour"),
        [
            pytest.param(-300, 5, id="five-hours-east-yekaterinburg"),
            pytest.param(-180, 7, id="three-hours-east-moscow"),
            pytest.param(180, 13, id="three-hours-west"),
            pytest.param(0, 10, id="utc"),
            pytest.param(-840, 20, id="fourteen-hours-east-kiritimati"),
        ],
    )
    def test_ten_in_the_morning_is_a_different_moment_in_each_timezone(
        self, offset: int, expected_hour: int
    ) -> None:
        local = datetime(2026, 9, 21, 10, 0)

        moment = to_utc(local, offset)

        assert moment.hour == expected_hour
        assert moment.tzinfo is UTC

    def test_the_offset_is_added_not_subtracted(self) -> None:
        """East of Greenwich the browser reports a negative offset."""
        assert to_utc(datetime(2026, 9, 21, 10, 0), -300) == datetime(2026, 9, 21, 5, 0, tzinfo=UTC)

    @pytest.mark.parametrize("offset", [841, -841, 100000])
    def test_an_impossible_timezone_is_refused(self, offset: int) -> None:
        with pytest.raises(InvalidInputError):
            to_utc(datetime(2026, 9, 21, 10, 0), offset)

    @pytest.mark.parametrize("offset", [840, -840])
    def test_the_furthest_real_timezones_are_accepted(self, offset: int) -> None:
        assert to_utc(datetime(2026, 9, 21, 10, 0), offset).tzinfo is UTC

    def test_a_time_that_already_has_a_timezone_is_refused(self) -> None:
        """The form sends wall-clock time. A time with a zone means the form changed."""
        with pytest.raises(InvalidInputError):
            to_utc(datetime(2026, 9, 21, 10, 0, tzinfo=UTC), -300)


class TestPrice:
    @pytest.mark.parametrize(
        ("typed", "minor"),
        [
            pytest.param("1500", 150_000, id="whole-roubles"),
            pytest.param("1500.50", 150_050, id="with-a-dot"),
            pytest.param("1500,50", 150_050, id="with-a-comma"),
            pytest.param("0.01", 1, id="one-kopeck"),
        ],
    )
    def test_roubles_become_kopecks(self, typed: str, minor: int) -> None:
        assert price_to_minor(typed) == minor

    @pytest.mark.parametrize("typed", ["", "0", "-1", "abc", "1500 ₽"])
    def test_a_price_we_refuse(self, typed: str) -> None:
        with pytest.raises(InvalidInputError):
            price_to_minor(typed)

    def test_kopecks_become_roubles_for_the_page(self) -> None:
        assert price_to_roubles(150_000) == "1500.00"
        assert price_to_roubles(1) == "0.01"


class TestWhatAPersonReadsInsteadOfAnErrorCode:
    @pytest.mark.parametrize(
        ("code", "text"),
        [
            pytest.param(
                "slot_already_booked",
                "Этот слот только что заняли. Выберите другое время.",
                id="the-race-a-person-can-lose",
            ),
            pytest.param("slot_in_past", "Это время уже прошло.", id="slot-in-past"),
            pytest.param("slot_not_found", "Такого слота больше нет.", id="slot-not-found"),
            pytest.param("booking_not_found", "Такой брони нет.", id="booking-not-found"),
            pytest.param(
                "booking_not_payable",
                "Эту бронь уже нельзя оплатить.",
                id="booking-not-payable",
            ),
            pytest.param(
                "invalid_transition",
                "Бронь уже в другом состоянии. Обновите страницу.",
                id="invalid-transition",
            ),
            pytest.param(
                "invalid_slot",
                "Слот должен начинаться в будущем и заканчиваться после начала.",
                id="invalid-slot",
            ),
            pytest.param(
                "payments_unavailable",
                "Оплата сейчас недоступна. Попробуйте через минуту.",
                id="payments-unavailable",
            ),
            pytest.param(
                "booking_unavailable",
                "Сервис записи сейчас недоступен. Попробуйте через минуту.",
                id="booking-unavailable",
            ),
        ],
    )
    def test_a_code_from_the_api_becomes_a_sentence(self, code: str, text: str) -> None:
        assert explain(code) == text

    @pytest.mark.parametrize("code", ["", "something_new", "internal_server_error"])
    def test_a_code_we_have_never_seen_still_says_something(self, code: str) -> None:
        assert explain(code) == UNEXPECTED
        assert code not in ERROR_MESSAGES
