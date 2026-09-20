"""Reading a JUnit XML report: what pytest wrote, and nothing about our storage.

Every level of the pipeline already writes this file (`make test-unit`,
`test-component`, `test-contract`, `smoke`), so it is the cheapest possible
source of data for the hub: no plugin in the product, no change to any service.

Two properties of the format are worth knowing, because ignoring either costs
us data:

* the status of a test is not an attribute, it is the presence of a child
  element (`failure`, `error`, `skipped`); a `testcase` without children
  passed. A test that broke during setup gets `error`, not `failure`, and the
  difference matters here: an `error` is far more often the infrastructure
  than the product;
* `xunit2` (the pytest default since 6.0) does not write the source file. We
  recover it from `classname` — the dotted module path plus the test class.
  When the attribute is present (`xunit1`), we take it instead of guessing.

Security: the report is uploaded to the hub by a CI step over the network, so
it is not a file we wrote, and it is exactly the case ruff rule S314 warns
about — `xml.etree.ElementTree` has no defence against entity expansion
(billion laughs) or an external entity that reads a file off the server.
Rather than silence the rule, the parser is `defusedxml`; only the *type*
`Element` comes from the standard library, which parses nothing. See ADR-0015.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import ParseError, fromstring


class CaseStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    # Broke outside the test body: a fixture, collection, teardown.
    ERROR = "error"


# Child element of `testcase` -> status. Order matters: a test can carry both a
# failure and an error (teardown broke after the body did), and the first match
# wins, because the earlier one is what a human has to investigate.
STATUS_BY_TAG: tuple[tuple[str, CaseStatus], ...] = (
    ("failure", CaseStatus.FAILED),
    ("error", CaseStatus.ERROR),
    ("skipped", CaseStatus.SKIPPED),
)

SOURCE_SUFFIX = ".py"


class JUnitParseError(Exception):
    """The file is not a JUnit report we can read. `code` goes to the API response."""

    code = "invalid_junit_report"


@dataclass(frozen=True)
class CaseResult:
    """One test in one report."""

    # `classname` as pytest wrote it: dotted module path plus the test class.
    classname: str
    # The test name, parametrisation in square brackets included.
    name: str
    # Source file: from the report when it is there, otherwise from `classname`.
    file: str
    status: CaseStatus
    # Seconds. A missing `time` is 0.0 — a report without timings is still a report.
    duration: float

    @property
    def test_id(self) -> str:
        """Identity of the test across runs. Parametrisation is part of it.

        One parameter can be flaky while the rest of the table is solid, and
        merging them hides exactly the case we are looking for.
        """
        return f"{self.classname}::{self.name}"


@dataclass(frozen=True)
class SuiteResult:
    """One `<testsuite>`: one pytest invocation, which for us is one level of the pyramid."""

    name: str
    cases: tuple[CaseResult, ...]
    duration: float


def file_from_classname(classname: str) -> str:
    """`tests.unit.test_domain.TestParsing` -> `tests/unit/test_domain.py`.

    Test classes are CamelCase and module names are not (PEP 8), so the first
    upper-case component ends the path.
    """
    path: list[str] = []
    for part in classname.split("."):
        if not part or part[:1].isupper():
            break
        path.append(part)
    if not path:
        return ""
    return "/".join(path) + SOURCE_SUFFIX


def _status_of(case: Element) -> CaseStatus:
    for tag, status in STATUS_BY_TAG:
        if case.find(tag) is not None:
            return status
    return CaseStatus.PASSED


def _seconds(raw: str | None) -> float:
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _parse_case(case: Element) -> CaseResult:
    name = case.get("name", "")
    if not name:
        raise JUnitParseError("a testcase without a name cannot be tracked")  # pragma: no mutate
    classname = case.get("classname", "")
    return CaseResult(
        classname=classname,
        name=name,
        file=case.get("file") or file_from_classname(classname),
        status=_status_of(case),
        duration=_seconds(case.get("time")),
    )


def _parse_suite(suite: Element) -> SuiteResult:
    return SuiteResult(
        name=suite.get("name", ""),
        cases=tuple(_parse_case(case) for case in suite.iter("testcase")),
        duration=_seconds(suite.get("time")),
    )


def parse_report(xml: str) -> tuple[SuiteResult, ...]:
    """Read a whole report. The root is `testsuites`, or a bare `testsuite`."""
    try:
        root: Element = fromstring(xml)
    except ParseError as error:
        raise JUnitParseError(f"report is not valid XML: {error}") from error  # pragma: no mutate

    if root.tag == "testsuite":
        return (_parse_suite(root),)
    if root.tag == "testsuites":
        return tuple(_parse_suite(suite) for suite in root.iter("testsuite"))
    raise JUnitParseError(f"root element is {root.tag!r}, not a JUnit report")  # pragma: no mutate


def parse_report_file(path: Path) -> tuple[SuiteResult, ...]:
    """The same, from a file: what the CI step uploading its own report uses."""
    return parse_report(path.read_text(encoding="utf-8"))


def cases_of(suites: tuple[SuiteResult, ...]) -> tuple[CaseResult, ...]:
    """Every test of every suite in one sequence, in report order."""
    return tuple(case for suite in suites for case in suite.cases)
