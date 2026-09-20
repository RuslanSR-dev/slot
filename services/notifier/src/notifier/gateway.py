"""Client of NotifyGate, the external notification gateway.

Its API is described in contracts/notifygw/openapi.yaml, which we treat as
received from another company: we cannot change it, and it is all we know
about them (ADR-0007).
"""

from dataclasses import dataclass

import httpx2

from notifier.domain import GatewayUnavailableError


@dataclass(frozen=True)
class Delivery:
    id: str


def message_request(recipient: str, text: str) -> dict[str, str]:
    """Body of POST /v1/messages. Checked against NotifyGate's schema in the tests."""
    return {"recipient": recipient, "text": text}


class NotifyGateway:
    def __init__(self, base_url: str, api_key: str, timeout: float = 5.0) -> None:
        self._http = httpx2.Client(
            base_url=base_url, timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}
        )

    def send(self, idempotency_key: str, recipient: str, text: str) -> Delivery:
        """Send one message. The key makes a repeat one message, not two (ADR-0010)."""
        try:
            response = self._http.post(
                "/v1/messages",
                headers={"Idempotency-Key": idempotency_key},
                json=message_request(recipient, text),
            )
        except httpx2.HTTPError as error:
            raise GatewayUnavailableError(f"NotifyGate did not respond: {error}") from error
        if response.status_code not in (200, 201):
            raise GatewayUnavailableError(f"NotifyGate answered {response.status_code}")
        return Delivery(id=response.json()["id"])

    def close(self) -> None:
        self._http.close()
