"""Client of PayStub, the external payment provider (contracts/paystub/openapi.yaml)."""

import uuid
from dataclasses import dataclass

import httpx2

from payments.domain import ProviderUnavailableError


@dataclass(frozen=True)
class Charge:
    id: str
    checkout_url: str


@dataclass(frozen=True)
class Refund:
    id: str
    status: str


def charge_request(payment_id: uuid.UUID, amount_minor: int, currency: str) -> dict[str, object]:
    """Body of POST /v1/charges. Checked against PayStub's schema in the tests."""
    return {"amount": amount_minor, "currency": currency, "reference": str(payment_id)}


def refund_request(charge_id: str, amount_minor: int) -> dict[str, object]:
    """Body of POST /v1/refunds. Checked against PayStub's schema in the tests."""
    return {"charge_id": charge_id, "amount": amount_minor}


class PayStubClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 5.0) -> None:
        self._http = httpx2.Client(
            base_url=base_url, timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}
        )

    def create_charge(self, payment_id: uuid.UUID, amount_minor: int, currency: str) -> Charge:
        try:
            response = self._http.post(
                "/v1/charges",
                # The provider returns the same charge for a repeated key: a retry after
                # a lost response does not charge the client twice (ADR-0008).
                headers={"Idempotency-Key": str(payment_id)},
                json=charge_request(payment_id, amount_minor, currency),
            )
        except httpx2.HTTPError as error:
            raise ProviderUnavailableError(f"PayStub did not respond: {error}") from error
        if response.status_code not in (200, 201):
            raise ProviderUnavailableError(f"PayStub answered {response.status_code}")
        body = response.json()
        return Charge(id=body["id"], checkout_url=body["checkout_url"])

    def create_refund(self, payment_id: uuid.UUID, charge_id: str, amount_minor: int) -> Refund:
        """Give the money back. Safe to repeat: the key is the payment id (ADR-0010)."""
        try:
            response = self._http.post(
                "/v1/refunds",
                headers={"Idempotency-Key": str(payment_id)},
                json=refund_request(charge_id, amount_minor),
            )
        except httpx2.HTTPError as error:
            raise ProviderUnavailableError(f"PayStub did not respond: {error}") from error
        if response.status_code not in (200, 201):
            raise ProviderUnavailableError(f"PayStub answered {response.status_code}")
        body = response.json()
        return Refund(id=body["id"], status=body["status"])

    def close(self) -> None:
        self._http.close()
