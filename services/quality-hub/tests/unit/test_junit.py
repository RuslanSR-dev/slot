"""Unit level: reading a report pytest really wrote, without a service around it.

`tests/fixtures/*.xml` are not hand-made XML. They are artifacts of real
`pytest --junitxml` runs of this repository, committed on purpose: a parser
tested only against XML the author invented proves that the author is
self-consistent, not that the parser reads pytest.
"""

from pathlib import Path

import pytest

from quality_hub.junit import (
    CaseStatus,
    JUnitParseError,
    cases_of,
    file_from_classname,
    parse_report,
    parse_report_file,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
# `make test-unit` of the notifier service: 20 tests, all green.
NOTIFIER_REPORT = FIXTURES / "notifier-junit-unit.xml"
# A run with a pass, a failure, a setup error and a skip in one file.
MIXED_REPORT = FIXTURES / "junit-mixed.xml"

MIXED_DEMO = "tests.unit.test_failures.TestQuarantineDemo"


def mixed() -> dict[str, CaseStatus]:
    cases = cases_of(parse_report_file(MIXED_REPORT))
    return {case.test_id: case.status for case in cases}


class TestRealReports:
    def test_a_real_pytest_report_yields_every_test_it_contains(self) -> None:
        suites = parse_report_file(NOTIFIER_REPORT)

        assert len(suites) == 1
        assert len(cases_of(suites)) == 20

    def test_a_report_where_nothing_failed_has_only_passed_tests(self) -> None:
        statuses = {case.status for case in cases_of(parse_report_file(NOTIFIER_REPORT))}

        assert statuses == {CaseStatus.PASSED}

    def test_the_suite_keeps_its_own_total_time(self) -> None:
        (suite,) = parse_report_file(NOTIFIER_REPORT)

        assert suite.name == "pytest"
        assert suite.duration == pytest.approx(0.293)

    def test_a_test_keeps_its_own_duration(self) -> None:
        cases = {case.name: case for case in cases_of(parse_report_file(NOTIFIER_REPORT))}

        assert cases["test_reads_the_fields_the_notifier_needs"].duration == pytest.approx(0.023)

    def test_parametrisation_stays_part_of_the_identity(self) -> None:
        """One parameter can be flaky while the rest of the table is solid."""
        ids = {case.test_id for case in cases_of(parse_report_file(NOTIFIER_REPORT))}

        assert (
            "tests.unit.test_domain.TestParsing::test_a_missing_field_is_not_an_event[event_id]"
            in ids
        )

    def test_the_source_file_is_recovered_from_the_dotted_class_name(self) -> None:
        """pytest's default report format (xunit2) does not write the file itself."""
        files = {case.file for case in cases_of(parse_report_file(NOTIFIER_REPORT))}

        assert files == {
            "tests/unit/test_domain.py",
            "tests/unit/test_health.py",
            "tests/unit/test_heartbeat.py",
        }


class TestStatuses:
    @pytest.mark.parametrize(
        ("test_name", "status"),
        [
            ("test_a_stable_test_passes", CaseStatus.PASSED),
            ("test_a_broken_test_fails", CaseStatus.FAILED),
            ("test_a_parametrised_test_fails_for_one_parameter[1]", CaseStatus.PASSED),
            ("test_a_parametrised_test_fails_for_one_parameter[2]", CaseStatus.FAILED),
            ("test_a_skipped_test_carries_no_signal", CaseStatus.SKIPPED),
        ],
    )
    def test_the_status_comes_from_the_child_element_not_an_attribute(
        self, test_name: str, status: CaseStatus
    ) -> None:
        assert mixed()[f"{MIXED_DEMO}::{test_name}"] == status

    def test_a_test_whose_fixture_broke_is_an_error_and_not_a_failure(self) -> None:
        """An error is far more often the infrastructure than the product."""
        assert (
            mixed()[f"{MIXED_DEMO}::test_a_test_whose_fixture_breaks_is_an_error"]
            == CaseStatus.ERROR
        )

    def test_a_failure_wins_over_a_later_error_in_the_same_test(self) -> None:
        """Teardown broke after the body did: the earlier problem is the one to fix."""
        xml = (
            '<testsuite name="pytest"><testcase classname="m" name="t" time="1">'
            '<failure message="boom" /><error message="teardown" />'
            "</testcase></testsuite>"
        )

        (case,) = cases_of(parse_report(xml))

        assert case.status == CaseStatus.FAILED


class TestShapeOfTheFile:
    def test_a_bare_testsuite_root_is_read_too(self) -> None:
        """Not every producer wraps the suite in <testsuites>."""
        xml = '<testsuite name="one"><testcase classname="m" name="t" time="0.5" /></testsuite>'

        (suite,) = parse_report(xml)

        assert suite.name == "one"
        assert suite.cases[0].duration == pytest.approx(0.5)

    def test_every_suite_of_a_multi_suite_report_is_read(self) -> None:
        xml = (
            '<testsuites name="all">'
            '<testsuite name="unit"><testcase classname="m" name="a" /></testsuite>'
            '<testsuite name="component"><testcase classname="m" name="b" /></testsuite>'
            "</testsuites>"
        )

        suites = parse_report(xml)

        assert [suite.name for suite in suites] == ["unit", "component"]
        assert [case.name for case in cases_of(suites)] == ["a", "b"]

    def test_the_file_attribute_is_preferred_when_the_producer_wrote_one(self) -> None:
        xml = (
            '<testsuite name="pytest"><testcase classname="tests.unit.test_x.TestY" '
            'name="t" file="tests/unit/test_x.py" /></testsuite>'
        )

        (case,) = cases_of(parse_report(xml))

        assert case.file == "tests/unit/test_x.py"

    def test_a_report_without_timings_is_still_a_report(self) -> None:
        xml = '<testsuite name="pytest"><testcase classname="m" name="t" /></testsuite>'

        (case,) = cases_of(parse_report(xml))

        assert case.duration == 0.0

    def test_an_unreadable_duration_does_not_lose_the_test(self) -> None:
        """A lost result is worse than a lost number: the status is what we count."""
        xml = '<testsuite name="pytest"><testcase classname="m" name="t" time="n/a" /></testsuite>'

        (case,) = cases_of(parse_report(xml))

        assert case.duration == 0.0
        assert case.status == CaseStatus.PASSED

    def test_a_report_with_no_tests_is_empty_and_not_an_error(self) -> None:
        assert cases_of(parse_report('<testsuites name="all" />')) == ()


class TestRefusals:
    def test_text_that_is_not_xml_is_refused(self) -> None:
        with pytest.raises(JUnitParseError):
            parse_report("<testsuite><unclosed>")

    def test_xml_that_is_not_a_report_is_refused(self) -> None:
        with pytest.raises(JUnitParseError) as error:
            parse_report("<coverage line-rate='0.9' />")

        assert "coverage" in str(error.value)

    def test_a_testcase_without_a_name_is_refused(self) -> None:
        """A run we cannot attribute to a test is not data, it is a silent hole."""
        with pytest.raises(JUnitParseError):
            parse_report('<testsuite name="pytest"><testcase classname="m" /></testsuite>')


class TestPathFromClassName:
    @pytest.mark.parametrize(
        ("classname", "expected"),
        [
            ("tests.unit.test_domain.TestParsing", "tests/unit/test_domain.py"),
            ("tests.unit.test_health", "tests/unit/test_health.py"),
            ("test_smoke", "test_smoke.py"),
            ("TestOnlyAClass", ""),
            ("", ""),
        ],
    )
    def test_the_first_camel_case_part_ends_the_path(self, classname: str, expected: str) -> None:
        assert file_from_classname(classname) == expected
