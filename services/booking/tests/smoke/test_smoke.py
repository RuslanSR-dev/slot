"""Smoke tests run against an already running stack (see `make up`).

They know nothing about the code: only the URL. The same file will later
check a deployed environment, not just the one inside the CI job.
"""

import os

import httpx2
import pytest

BASE_URL = os.environ.get("SLOT_BOOKING_URL", "http://127.0.0.1:8000")
# Empty counts as missing: `make` passes an empty value when git is unavailable.
EXPECTED_BUILD_SHA = os.environ.get("SLOT_EXPECTED_BUILD_SHA") or None
# GitHub Actions and most CI systems set CI=true.
IN_CI = os.environ.get("CI") == "true"


def test_booking_is_up() -> None:
    response = httpx2.get(f"{BASE_URL}/health", timeout=5)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "booking"


def test_running_build_is_the_one_we_just_built() -> None:
    if EXPECTED_BUILD_SHA is None:
        # A skipped gate looks green. In CI that would silently switch this check off.
        if IN_CI:
            pytest.fail("SLOT_EXPECTED_BUILD_SHA must be set in CI, otherwise this gate is off")
        pytest.skip("SLOT_EXPECTED_BUILD_SHA is not set; allowed only outside CI")

    response = httpx2.get(f"{BASE_URL}/health", timeout=5)

    assert response.json()["build_sha"] == EXPECTED_BUILD_SHA
