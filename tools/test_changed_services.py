"""Test impact analysis decides which gates run: it is tested like any gate."""

import pytest

from changed_services import SERVICES, affected_services

ALL = sorted(SERVICES)


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        pytest.param(["services/booking/src/booking/app.py"], ["booking"], id="own-code"),
        pytest.param(["services/payments/uv.lock"], ["payments"], id="own-dependencies"),
        pytest.param(
            ["services/booking/app.py", "services/payments/Dockerfile"],
            ["booking", "payments"],
            id="two-services",
        ),
        pytest.param(["services/notifier/src/notifier/consumer.py"], ["notifier"], id="notifier"),
        pytest.param(["contracts/pacts/booking-payments.json"], ["payments"], id="pact-provider"),
        pytest.param(["contracts/pacts/payments-booking.json"], ["booking"], id="pact-reverse"),
        pytest.param(["contracts/paystub/openapi.yaml"], ["payments"], id="external-api"),
        pytest.param(["contracts/notifygw/openapi.yaml"], ["notifier"], id="external-api-notifygw"),
        pytest.param(
            ["contracts/events/booking.v1.json"], ["booking", "notifier"], id="event-schema"
        ),
        pytest.param(
            ["contracts/events/consumers/notifier.json"],
            ["booking", "notifier"],
            id="event-consumer-contract",
        ),
        pytest.param(["services/web/src/web/app.py"], ["web"], id="web"),
        pytest.param(
            ["contracts/auth/token.v1.json"], ["booking", "web"], id="token-format-contract"
        ),
        pytest.param(["docs/roadmap.md", "README.md"], [], id="docs-only"),
        pytest.param(["smoke/tests/test_stack.py"], [], id="smoke-runs-anyway"),
        pytest.param(["e2e/tests/test_booking_flow.py"], [], id="e2e-runs-anyway"),
        pytest.param([], [], id="nothing-changed"),
    ],
)
def test_affected_services(paths: list[str], expected: list[str]) -> None:
    assert affected_services(paths) == expected


@pytest.mark.parametrize(
    "path",
    [
        "Makefile",
        "compose.yaml",
        ".github/workflows/ci.yml",
        "infra/paystub/mappings/create-charge.json",
        "infra/notifygw/mappings/send-message.json",
        "tools/changed_services.py",
        "services/new-service/app.py",
        "<unknown base>",
    ],
)
def test_shared_or_unknown_files_run_everything(path: str) -> None:
    """A wrong "nothing to run" is far worse than an extra run."""
    assert affected_services(["docs/x.md", path]) == ALL


def test_pact_with_an_unknown_provider_runs_everything() -> None:
    assert affected_services(["contracts/pacts/booking-search.json"]) == ALL
