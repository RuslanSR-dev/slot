"""Who is calling, proved by a signature. No passwords, no accounts.

The web service signs a short-lived token with a secret shared through the
environment; booking verifies the signature and reads from the token who
came. The token format is a contract between two services that must not
import each other (ADR-0002), so it is pinned by the vectors in
contracts/auth/token.v1.json and checked from both sides.

This module only *verifies*: it cannot mint a token. Tests build tokens
byte by byte, so a mutant that breaks verification cannot be covered up by
a matching bug in the issuer - which is exactly what a round trip through
our own signing code would do.

Error messages are marked `pragma: no mutate`: the contract is the error
`code`, the message is a hint for humans.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from booking.domain import BookingNotFoundError, DomainError
from booking.schemas import EXTERNAL_ID_PATTERN, MAX_EXTERNAL_ID_LENGTH

# The version is part of the signed message: a token of another format
# cannot be replayed as this one.
VERSION_PREFIX = "v1"
SEPARATOR = "."
PART_COUNT = 3
DIGEST = hashlib.sha256
# base64url in the token carries no padding: an "=" in a cookie value makes
# the server quote it, and the quotes come back inside the token. Decoding
# needs the padding again, and base64 ignores more of it than it needs.
PADDING = "==="
# Claim names. Short on purpose: the token travels in a header and a cookie.
SUBJECT_CLAIM = "sub"
ROLE_CLAIM = "role"
EXPIRES_CLAIM = "exp"
# The subject is an identifier of a person in the same alphabet as every other
# identifier we accept over HTTP (schemas.ExternalId).
SUBJECT = re.compile(EXTERNAL_ID_PATTERN)


class Role(StrEnum):
    CLIENT = "client"
    MASTER = "master"


class AuthError(DomainError):
    """The caller did not prove who they are. Always answered with 401."""

    code = "unauthorized"


class MissingTokenError(AuthError):
    code = "unauthorized"


class InvalidTokenError(AuthError):
    code = "invalid_token"


class ExpiredTokenError(AuthError):
    """The signature is ours, but the token is too old. The client can log in again."""

    code = "token_expired"


class ForbiddenError(DomainError):
    """A known caller in the wrong role. Answered with 403: nothing to hide."""

    code = "forbidden"


@dataclass(frozen=True)
class Identity:
    subject: str
    role: Role


def signature(payload: str, secret: str) -> str:
    """Sign `v1.<payload>`: the version is signed together with the claims."""
    message = f"{VERSION_PREFIX}{SEPARATOR}{payload}".encode()
    digest = hmac.new(secret.encode(), message, DIGEST).digest()
    return base64.urlsafe_b64encode(digest).replace(b"=", b"").decode()


def verify_token(token: str, secret: str, now: datetime) -> Identity:
    """The identity inside the token, or an error saying why it is not trusted."""
    parts = token.split(SEPARATOR)
    if len(parts) != PART_COUNT:
        raise InvalidTokenError("token must have three parts")  # pragma: no mutate
    version, payload, presented = parts
    if version != VERSION_PREFIX:
        raise InvalidTokenError(f"unknown token version {version}")  # pragma: no mutate
    # Constant-time compare: a fast "wrong at byte 3" answer leaks the signature.
    if not hmac.compare_digest(signature(payload, secret), presented):
        raise InvalidTokenError("signature does not match")  # pragma: no mutate
    return _identity(_claims(payload), now)


def _claims(payload: str) -> dict[str, object]:
    """Read the claims. The signature is already checked, so this is our own data."""
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + PADDING))
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise InvalidTokenError(f"payload is not readable: {error}") from error  # pragma: no mutate
    if not isinstance(claims, dict):
        raise InvalidTokenError("payload is not an object")  # pragma: no mutate
    return claims


def _identity(claims: dict[str, object], now: datetime) -> Identity:
    subject, role, expires_at = (
        claims.get(SUBJECT_CLAIM),
        claims.get(ROLE_CLAIM),
        claims.get(EXPIRES_CLAIM),
    )
    if not isinstance(subject, str) or not subject:
        raise InvalidTokenError("token has no subject")  # pragma: no mutate
    # The subject becomes `client_id` in the database and a filter in queries.
    # A signature says the token is ours, not that its contents are safe.
    if len(subject) > MAX_EXTERNAL_ID_LENGTH or not SUBJECT.fullmatch(subject):
        raise InvalidTokenError("subject is not an identifier")  # pragma: no mutate
    try:
        known_role = Role(str(role))
    except ValueError as error:
        raise InvalidTokenError(f"unknown role {role}") from error  # pragma: no mutate
    if not isinstance(expires_at, int):
        raise InvalidTokenError("token has no expiry")  # pragma: no mutate
    # A token that expires exactly now is expired: the boundary belongs to the past.
    if expires_at <= now.timestamp():
        raise ExpiredTokenError("token expired")  # pragma: no mutate
    return Identity(subject=subject, role=known_role)


def ensure_owner(identity: Identity, client_id: str, master_id: str) -> None:
    """A client owns their bookings, a master owns the bookings on their slots.

    Someone else's booking is answered with 404, not 403: a 403 would confirm
    that this booking id exists, and identifiers are the only thing between a
    stranger and the schedule of a real person.
    """
    owner = client_id if identity.role is Role.CLIENT else master_id
    if identity.subject != owner:
        raise BookingNotFoundError("booking does not exist")  # pragma: no mutate


def ensure_role(identity: Identity, required: Role) -> None:
    """The operation belongs to the other role. 403: the rule is public, nothing leaks."""
    if identity.role is not required:
        raise ForbiddenError(f"this operation is for a {required}")  # pragma: no mutate
