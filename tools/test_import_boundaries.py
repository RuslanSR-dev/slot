"""A fitness function nobody has seen fail is a decoration: these tests break the
architectural rule on purpose and demand a red result, and they also show which
imports must stay legal, so the gate cannot be made green by forbidding everything."""

from pathlib import Path

import pytest

from import_boundaries import REPO_ROOT, find_violations, main


@pytest.fixture
def services(tmp_path: Path) -> Path:
    """Two services shaped like the real ones: services/<name>/src/<name>."""
    root = tmp_path / "services"
    for name in ("booking", "payments"):
        write(root / name / "src" / name / "__init__.py", "")
    return root


def write(path: Path, code: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    return path


def test_a_service_that_imports_the_package_of_another_service_is_a_violation(
    services: Path,
) -> None:
    write(services / "booking" / "src" / "booking" / "app.py", "from payments.domain import Price")

    [violation] = find_violations(services)
    assert (violation.line, violation.imported, violation.owner) == (1, "payments", "payments")


def test_a_plain_import_of_another_service_is_a_violation_too(services: Path) -> None:
    """`import payments` binds the two services exactly as hard as `from payments import x`."""
    write(services / "booking" / "src" / "booking" / "app.py", "import uuid\nimport payments.api\n")

    [violation] = find_violations(services)
    assert (violation.line, violation.imported) == (2, "payments")


def test_a_forbidden_import_in_a_test_of_the_service_is_a_violation(services: Path) -> None:
    """A test that imports the neighbour cannot run without it either."""
    path = write(
        services / "payments" / "tests" / "unit" / "test_domain.py",
        "import pytest\n\nfrom booking.domain import Booking\n",
    )

    [violation] = find_violations(services)
    assert (violation.path, violation.line, violation.owner) == (path, 3, "booking")


def test_the_command_exits_with_1_and_names_file_line_and_import(
    services: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write(services / "booking" / "src" / "booking" / "app.py", "\n\nfrom payments import domain\n")
    monkeypatch.setattr("sys.argv", ["import_boundaries.py", str(services)])

    assert main() == 1
    out = capsys.readouterr().out
    assert "booking/src/booking/app.py:3: imports 'payments' of service payments" in out


def test_own_package_third_party_and_standard_library_imports_are_allowed(services: Path) -> None:
    write(
        services / "booking" / "src" / "booking" / "app.py",
        "import uuid\nimport fastapi\nfrom booking.domain import Booking\nimport booking.outbox\n",
    )

    assert find_violations(services) == []


def test_relative_imports_are_allowed(services: Path) -> None:
    """Component tests import their own helpers as `from .wiremock import WireMock`;
    a relative import cannot leave the service in the first place."""
    write(
        services / "payments" / "tests" / "component" / "conftest.py",
        "from .wiremock import WireMock\nfrom ..component.wiremock import WireMock as W\n",
    )

    assert find_violations(services) == []


def test_a_service_name_in_a_string_or_a_comment_is_not_an_import(services: Path) -> None:
    """The reason for parsing the tree instead of grepping: pact files, URLs and
    prose mention the neighbour all the time, and none of that is a dependency."""
    write(
        services / "booking" / "src" / "booking" / "app.py",
        "# calls payments over HTTP\n"
        'URL = "http://payments:8000"\n'
        'NAME = "from payments import x"\n',
    )

    assert find_violations(services) == []


def test_the_repository_itself_has_no_cross_service_imports() -> None:
    """The gate is pointed at the real services, not only at temporary files."""
    assert find_violations(REPO_ROOT / "services") == []
