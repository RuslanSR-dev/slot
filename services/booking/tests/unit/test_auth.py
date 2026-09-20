"""Token verification: pure logic, so it is checked here and mutated by mutmut.

The tokens are built here by hand, not by our own signing code: booking
never signs anything. A round trip through a shared helper would pass even
if both sides were wrong in the same way.
"""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from booking.auth import (
    ExpiredTokenError,
    ForbiddenError,
    Identity,
    InvalidTokenError,
    Role,
    ensure_owner,
    ensure_role,
    verify_token,
)
from booking.domain import BookingNotFoundError

SECRET = "test-secret"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
LATER = int((NOW + timedelta(hours=1)).timestamp())
VECTOR_FILE = Path("contracts") / "auth" / "token.v1.json"


def vectors() -> Any:
    """Found by walking up: mutmut runs these tests from a copy of the tree."""
    for parent in Path(__file__).resolve().parents:
        if (parent / VECTOR_FILE).exists():
            return json.loads((parent / VECTOR_FILE).read_text())
    raise AssertionError(f"{VECTOR_FILE} not found above {__file__}")


VECTORS = vectors()


def recorded_token(vector: Any) -> str:
    """The file keeps the two parts apart so that no committed string looks
    like a live token; the token itself is what web actually issues."""
    return f"v1.{vector['claims']}.{vector['signature']}"


def b64(raw: bytes) -> str:
    """base64url, no padding: the token travels in a cookie (see auth.PADDING)."""
    return base64.urlsafe_b64encode(raw).replace(b"=", b"").decode()


def sign(payload: str, secret: str = SECRET) -> str:
    return b64(hmac.new(secret.encode(), f"v1.{payload}".encode(), hashlib.sha256).digest())


def token(claims: Any, secret: str = SECRET, version: str = "v1") -> str:
    """A token as the web service would build it, written independently here."""
    payload = b64(json.dumps(claims).encode())
    return f"{version}.{payload}.{sign(payload, secret)}"


def valid_claims(**changes: Any) -> dict[str, Any]:
    return {"sub": "anna", "role": "client", "exp": LATER} | changes


class TestTokenFormatIsTheContractBetweenWebAndBooking:
    """contracts/auth/token.v1.json: web writes these bytes, booking reads them."""

    @pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["subject"])
    def test_a_recorded_token_is_read_as_its_recorded_identity(self, vector: Any) -> None:
        identity = verify_token(recorded_token(vector), VECTORS["secret"], NOW)

        assert identity == Identity(subject=vector["subject"], role=Role(vector["role"]))

    @pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["subject"])
    def test_a_recorded_token_is_refused_after_its_recorded_expiry(self, vector: Any) -> None:
        after = datetime.fromtimestamp(vector["expires_at"] + 1, tz=UTC)

        with pytest.raises(ExpiredTokenError):
            verify_token(recorded_token(vector), VECTORS["secret"], after)


class TestSignature:
    def test_a_token_we_signed_names_the_caller(self) -> None:
        identity = verify_token(token(valid_claims(sub="boris", role="master")), SECRET, NOW)

        assert identity == Identity(subject="boris", role=Role.MASTER)

    def test_a_token_signed_with_another_secret_is_refused(self) -> None:
        with pytest.raises(InvalidTokenError):
            verify_token(token(valid_claims(), secret="attacker-secret"), SECRET, NOW)

    def test_changed_claims_break_the_signature(self) -> None:
        """The whole point: the payload cannot be edited without the secret."""
        presented = token(valid_claims(sub="anna")).split(".")[2]
        forged_payload = b64(json.dumps(valid_claims(sub="boris")).encode())

        with pytest.raises(InvalidTokenError):
            verify_token(f"v1.{forged_payload}.{presented}", SECRET, NOW)

    def test_a_signature_of_the_payload_alone_is_refused(self) -> None:
        """The version is signed too, so a v2 token cannot be replayed as v1."""
        payload = b64(json.dumps(valid_claims()).encode())
        without_version = b64(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest())

        with pytest.raises(InvalidTokenError):
            verify_token(f"v1.{payload}.{without_version}", SECRET, NOW)

    @pytest.mark.parametrize(
        "broken",
        [
            pytest.param("", id="empty"),
            pytest.param("v1", id="one-part"),
            pytest.param("v1.payload", id="two-parts"),
            pytest.param("v1.payload.signature.extra", id="four-parts"),
        ],
    )
    def test_a_token_of_the_wrong_shape_is_refused(self, broken: str) -> None:
        with pytest.raises(InvalidTokenError):
            verify_token(broken, SECRET, NOW)

    @pytest.mark.parametrize("version", ["v2", "V1", "", "v1 "])
    def test_a_token_of_another_version_is_refused(self, version: str) -> None:
        with pytest.raises(InvalidTokenError):
            verify_token(token(valid_claims(), version=version), SECRET, NOW)


class TestClaims:
    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("not-base64!", id="not-base64"),
            pytest.param(b64(b"not json"), id="not-json"),
            pytest.param(b64(b'["anna"]'), id="json-but-not-an-object"),
            pytest.param(b64(b"\xff\xfe"), id="not-text"),
        ],
    )
    def test_a_signed_but_unreadable_payload_is_refused(self, payload: str) -> None:
        """Our own signature over rubbish is a bug, not an attack - but not a 500."""
        with pytest.raises(InvalidTokenError):
            verify_token(f"v1.{payload}.{sign(payload)}", SECRET, NOW)

    @pytest.mark.parametrize(
        "claims",
        [
            pytest.param({"role": "client", "exp": LATER}, id="no-subject"),
            pytest.param(valid_claims(sub=""), id="empty-subject"),
            pytest.param(valid_claims(sub=42), id="subject-is-not-text"),
            pytest.param(valid_claims(sub=None), id="subject-is-null"),
            pytest.param({"sub": "anna", "exp": LATER}, id="no-role"),
            pytest.param(valid_claims(role="admin"), id="unknown-role"),
            pytest.param(valid_claims(role="CLIENT"), id="role-in-another-case"),
            pytest.param({"sub": "anna", "role": "client"}, id="no-expiry"),
            pytest.param(valid_claims(exp="soon"), id="expiry-is-not-a-number"),
            pytest.param(valid_claims(sub="anna\x00"), id="nul-byte-in-subject"),
            pytest.param(valid_claims(sub="anna smith"), id="space-in-subject"),
            pytest.param(valid_claims(sub="anna\n"), id="newline-after-subject"),
            pytest.param(valid_claims(sub="a" * 65), id="subject-too-long"),
        ],
    )
    def test_a_token_without_the_claims_we_need_is_refused(self, claims: dict[str, Any]) -> None:
        with pytest.raises(InvalidTokenError):
            verify_token(token(claims), SECRET, NOW)

    def test_a_subject_of_exactly_the_maximum_length_is_accepted(self) -> None:
        """The boundary of the identifier alphabet: 64 characters, not 65."""
        longest = "a" * 64

        assert verify_token(token(valid_claims(sub=longest)), SECRET, NOW).subject == longest

    def test_a_token_that_expires_exactly_now_is_expired(self) -> None:
        """The boundary belongs to the past: `<=`, not `<`."""
        with pytest.raises(ExpiredTokenError):
            verify_token(token(valid_claims(exp=int(NOW.timestamp()))), SECRET, NOW)

    def test_a_token_that_expires_a_second_later_still_works(self) -> None:
        exactly_now = int(NOW.timestamp())

        identity = verify_token(token(valid_claims(exp=exactly_now + 1)), SECRET, NOW)

        assert identity.subject == "anna"

    def test_an_expired_token_is_told_apart_from_a_forged_one(self) -> None:
        """Different codes: an expired token means "log in again", a forged one does not."""
        expired = token(valid_claims(exp=int(NOW.timestamp()) - 1))

        with pytest.raises(ExpiredTokenError) as error:
            verify_token(expired, SECRET, NOW)

        assert error.value.code == "token_expired"


class TestOwnership:
    @pytest.mark.parametrize(
        "identity",
        [
            pytest.param(Identity("anna", Role.CLIENT), id="client-owns-the-booking"),
            pytest.param(Identity("masha", Role.MASTER), id="master-owns-the-slot"),
        ],
    )
    def test_both_sides_of_a_booking_may_touch_it(self, identity: Identity) -> None:
        ensure_owner(identity, client_id="anna", master_id="masha")

    @pytest.mark.parametrize(
        "identity",
        [
            pytest.param(Identity("boris", Role.CLIENT), id="another-client"),
            pytest.param(Identity("petr", Role.MASTER), id="another-master"),
            pytest.param(Identity("masha", Role.CLIENT), id="master-name-but-client-role"),
            pytest.param(Identity("anna", Role.MASTER), id="client-name-but-master-role"),
        ],
    )
    def test_nobody_else_may_touch_it(self, identity: Identity) -> None:
        with pytest.raises(BookingNotFoundError):
            ensure_owner(identity, client_id="anna", master_id="masha")

    def test_a_stranger_is_told_the_booking_does_not_exist(self) -> None:
        """404, not 403: a 403 would confirm that this booking id exists."""
        with pytest.raises(BookingNotFoundError) as error:
            ensure_owner(Identity("boris", Role.CLIENT), client_id="anna", master_id="masha")

        assert error.value.code == "booking_not_found"


class TestRoles:
    @pytest.mark.parametrize("role", list(Role))
    def test_the_required_role_passes(self, role: Role) -> None:
        ensure_role(Identity("anna", role), role)

    @pytest.mark.parametrize(
        ("has", "required"),
        [
            pytest.param(Role.CLIENT, Role.MASTER, id="client-publishing-slots"),
            pytest.param(Role.MASTER, Role.CLIENT, id="master-booking-slots"),
        ],
    )
    def test_the_other_role_is_forbidden(self, has: Role, required: Role) -> None:
        with pytest.raises(ForbiddenError) as error:
            ensure_role(Identity("anna", has), required)

        assert error.value.code == "forbidden"
