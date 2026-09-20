"""A thin client of WireMock's admin API: stubs a neighbour service over real HTTP.

Duplicated in each service on purpose: services share no code (ADR-0002).
"""

from typing import Any

import httpx2

WIREMOCK_IMAGE = "wiremock/wiremock:3.13.1"


class WireMock:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._admin = httpx2.Client(base_url=f"{base_url}/__admin", timeout=5)

    def is_ready(self) -> bool:
        try:
            return self._admin.get("/health").status_code == 200
        except httpx2.HTTPError:
            return False

    def reset(self) -> None:
        self._admin.post("/reset").raise_for_status()

    def stub(
        self,
        method: str,
        url_path: str,
        *,
        pattern: bool = False,
        status: int = 200,
        json_body: Any = None,
        delay_ms: int = 0,
        fault: str | None = None,
    ) -> None:
        """Answer `method url_path` with a response, a delay or a network fault.

        Faults are real network failures, e.g. CONNECTION_RESET_BY_PEER.
        With `pattern=True`, `url_path` is a regular expression.
        """
        response: dict[str, Any] = {"status": status}
        if json_body is not None:
            response["jsonBody"] = json_body
        if delay_ms:
            response["fixedDelayMilliseconds"] = delay_ms
        if fault is not None:
            response = {"fault": fault}
        path_key = "urlPathPattern" if pattern else "urlPath"
        mapping = {"request": {"method": method, path_key: url_path}, "response": response}
        self._admin.post("/mappings", json=mapping).raise_for_status()

    def received(
        self, method: str, url_path: str, *, pattern: bool = False
    ) -> list[dict[str, Any]]:
        """Requests the stub actually got, to check what our service sent."""
        path_key = "urlPathPattern" if pattern else "urlPath"
        response = self._admin.post("/requests/find", json={"method": method, path_key: url_path})
        response.raise_for_status()
        requests: list[dict[str, Any]] = response.json()["requests"]
        return requests

    def close(self) -> None:
        self._admin.close()
