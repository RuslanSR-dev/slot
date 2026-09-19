import pytest

from payments.domain import (
    ALLOWED_TRANSITIONS,
    InvalidSignatureError,
    PaymentStatus,
    can_transition,
    sign,
    target_status,
    verify_signature,
)

SECRET = "webhook-secret"
BODY = b'{"id":"evt_1","type":"charge.succeeded"}'


class TestSignature:
    def test_body_signed_with_the_shared_secret_is_accepted(self) -> None:
        verify_signature(BODY, sign(BODY, SECRET), SECRET)

    def test_signature_has_the_documented_format(self) -> None:
        signature = sign(BODY, SECRET)

        assert signature.startswith("sha256=")
        assert len(signature.removeprefix("sha256=")) == 64

    def test_known_signature_value(self) -> None:
        """Pins the algorithm: HMAC-SHA256 over the raw body, hex-encoded.

        Expected value computed independently: printf payload | openssl dgst -sha256 -hmac key
        """
        assert sign(b"payload", "key") == (
            "sha256=5d98b45c90a207fa998ce639fea6f02ecc8cc3f36fef81d694fb856b4d0a28ca"
        )

    @pytest.mark.parametrize(
        ("body", "header"),
        [
            pytest.param(BODY, None, id="missing-header"),
            pytest.param(BODY, sign(BODY, "other-secret"), id="wrong-secret"),
            pytest.param(BODY + b" ", sign(BODY, SECRET), id="body-changed-after-signing"),
            pytest.param(BODY, sign(BODY, SECRET).removeprefix("sha256="), id="no-prefix"),
            pytest.param(BODY, "", id="empty-header"),
        ],
    )
    def test_unsigned_or_tampered_webhook_is_rejected(
        self, body: bytes, header: str | None
    ) -> None:
        with pytest.raises(InvalidSignatureError):
            verify_signature(body, header, SECRET)


class TestEvents:
    @pytest.mark.parametrize(
        ("event_type", "status"),
        [
            ("charge.succeeded", PaymentStatus.SUCCEEDED),
            ("charge.failed", PaymentStatus.FAILED),
        ],
    )
    def test_known_event_moves_payment_to_status(
        self, event_type: str, status: PaymentStatus
    ) -> None:
        assert target_status(event_type) == status

    @pytest.mark.parametrize("event_type", ["charge.refunded", "customer.created", ""])
    def test_other_events_are_not_acted_on(self, event_type: str) -> None:
        assert target_status(event_type) is None


class TestTransitions:
    @pytest.mark.parametrize("target", [PaymentStatus.SUCCEEDED, PaymentStatus.FAILED], ids=str)
    def test_pending_payment_can_finish(self, target: PaymentStatus) -> None:
        assert can_transition(PaymentStatus.PENDING, target) is True

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (current, target)
            for current in PaymentStatus
            for target in PaymentStatus
            if target not in ALLOWED_TRANSITIONS[current]
        ],
    )
    def test_every_other_transition_is_refused(
        self, current: PaymentStatus, target: PaymentStatus
    ) -> None:
        assert can_transition(current, target) is False
