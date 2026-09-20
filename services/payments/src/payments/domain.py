"""Payment rules that do not depend on the database, HTTP or the provider.

Error messages are marked `pragma: no mutate`: the API contract is the error
`code`, the message is a hint for humans. Tests pin codes, not wording.
"""

import hashlib
import hmac
from enum import StrEnum


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ALLOWED_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.PENDING: frozenset({PaymentStatus.SUCCEEDED, PaymentStatus.FAILED}),
    PaymentStatus.SUCCEEDED: frozenset(),
    PaymentStatus.FAILED: frozenset(),
}

# Provider events we act on. Any other event type is acknowledged and ignored.
EVENT_TARGET_STATUS: dict[str, PaymentStatus] = {
    "charge.succeeded": PaymentStatus.SUCCEEDED,
    "charge.failed": PaymentStatus.FAILED,
}

SIGNATURE_PREFIX = "sha256="

# Commands this service writes into its outbox for the relay to deliver (ADR-0010).
CONFIRM_BOOKING = "booking.confirm"
REFUND_PAYMENT = "payment.refund"


class DomainError(Exception):
    """A request that breaks a business rule. `code` goes to the API response."""

    code: str = "domain_error"


class InvalidSignatureError(DomainError):
    code = "invalid_signature"


class InvalidEventError(DomainError):
    code = "invalid_event"


class PaymentNotFoundError(DomainError):
    code = "payment_not_found"


class PaymentConflictError(DomainError):
    code = "payment_conflict"


class ProviderUnavailableError(DomainError):
    code = "provider_unavailable"


class BookingUnavailableError(DomainError):
    code = "booking_unavailable"


class BookingRejectedError(DomainError):
    code = "booking_rejected"


def sign(body: bytes, secret: str) -> str:
    """The value PayStub puts into the `PayStub-Signature` header."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(body: bytes, header: str | None, secret: str) -> None:
    """Reject a webhook that PayStub did not send, or that was changed on the way."""
    if header is None:
        raise InvalidSignatureError("signature header is missing")  # pragma: no mutate
    # Constant-time comparison: a plain == leaks how many characters matched.
    if not hmac.compare_digest(header, sign(body, secret)):
        raise InvalidSignatureError("signature does not match the body")  # pragma: no mutate


def target_status(event_type: str) -> PaymentStatus | None:
    return EVENT_TARGET_STATUS.get(event_type)


def can_transition(current: PaymentStatus, target: PaymentStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]
