"""The policy decides what a finding means; these tests are that policy.

The scanner itself is somebody else's code and is not tested here. What is
tested is everything we put on top of it: that one vulnerability reported
under three identifiers is one finding, that a severity nobody rated blocks
instead of passing, that the deadline is counted from the day the advisory
became public, and that an acceptance nobody needs any more is not allowed to
sit in the file looking like a decision.
"""

from datetime import date, timedelta
from typing import Any

import pytest

from accepted_risks import AcceptedRisk
from dependency_audit import (
    GRACE_DAYS,
    Finding,
    findings_of,
    judge,
    unused_acceptances,
)

TODAY = date(2026, 9, 20)
REASON = (
    "WireMock only; the parser is never given data from outside the test, and"
    " the fix needs a major upgrade planned for the next iteration."
)


def vulnerability(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "dependency": {"name": "jinja2", "version": "2.10"},
        "id": "GHSA-462w-v97r-4m45",
        "aliases": ["CVE-2019-10906", "PYSEC-2019-217"],
        "summary": "Sandbox escape",
        "fix_versions": ["2.10.1"],
        "published": "2019-04-10T00:00:00Z",
        "link": "https://github.com/advisories/GHSA-462w-v97r-4m45",
    }
    raw.update(overrides)
    return raw


def report(*vulnerabilities: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": {"audited_packages": 1, "vulnerabilities": len(vulnerabilities)},
        "vulnerabilities": list(vulnerabilities),
    }


def finding(**overrides: Any) -> Finding:
    values: dict[str, Any] = {
        "project": "booking",
        "package": "jinja2",
        "version": "2.10",
        "advisory": "GHSA-462w-v97r-4m45",
        "aliases": ("CVE-2019-10906",),
        "summary": "Sandbox escape",
        "fix_versions": ("2.10.1",),
        "published": date(2019, 4, 10),
        "link": "https://example.invalid",
    }
    values.update(overrides)
    return Finding(**values)


def acceptance(**overrides: Any) -> AcceptedRisk:
    values: dict[str, Any] = {
        "id": "CVE-2019-10906",
        "package": "jinja2",
        "reason": REASON,
        "owner": "Ruslan",
        "added": date(2026, 9, 1),
        "until": date(2026, 11, 1),
        "link": "https://example.invalid",
        "no_fix": False,
    }
    values.update(overrides)
    return AcceptedRisk(**values)


class TestReading:
    def test_one_vulnerability_under_three_identifiers_is_one_finding(self) -> None:
        """OSV publishes the same hole as GHSA, PYSEC and CVE, and the audit
        prints all three. Counting them as three would make every report look
        three times worse than it is."""
        document = report(
            vulnerability(),
            vulnerability(id="PYSEC-2019-217", aliases=["CVE-2019-10906"]),
        )

        [found] = findings_of("booking", document)

        assert found.advisory == "GHSA-462w-v97r-4m45"

    def test_the_github_record_wins_because_only_it_carries_a_severity(self) -> None:
        document = report(
            vulnerability(id="PYSEC-2019-217", aliases=["CVE-2019-10906"]),
            vulnerability(),
        )

        [found] = findings_of("booking", document)

        assert found.advisory == "GHSA-462w-v97r-4m45"

    def test_the_same_advisory_in_two_packages_stays_two_findings(self) -> None:
        document = report(
            vulnerability(),
            vulnerability(dependency={"name": "other", "version": "1.0"}),
        )

        assert len(findings_of("booking", document)) == 2

    def test_an_output_we_no_longer_understand_is_an_error(self) -> None:
        """`uv audit` is in preview. A gate that reads an unknown format as
        "no vulnerabilities" is worse than no gate at all."""
        with pytest.raises(ValueError, match="format has probably changed"):
            findings_of("booking", {"summary": {}, "issues": []})

    def test_a_vulnerability_without_a_fix_is_recognised(self) -> None:
        [found] = findings_of("booking", report(vulnerability(fix_versions=[])))

        assert not found.has_fix


class TestPolicy:
    @pytest.mark.parametrize("severity", ["CRITICAL", "HIGH"])
    def test_a_severe_finding_blocks_at_once(self, severity: str) -> None:
        assert judge(finding(), severity, [], TODAY).status == "blocks"

    def test_a_severity_nobody_rated_blocks_too(self) -> None:
        """Failing closed: a gate that guesses "probably low" is not a gate."""
        assert judge(finding(), "UNKNOWN", [], TODAY).status == "blocks"

    def test_a_fresh_moderate_finding_is_shown_but_does_not_block(self) -> None:
        """Nobody should have a pull request broken by an advisory published
        while it was open - that is how teams learn to bypass the gate."""
        fresh = finding(published=TODAY - timedelta(days=1))

        judgement = judge(fresh, "MODERATE", [], TODAY)

        assert judgement.status == "reported"
        assert f"{GRACE_DAYS - 1} days left" in judgement.explanation

    def test_a_moderate_finding_blocks_once_the_deadline_has_passed(self) -> None:
        late = finding(published=TODAY - timedelta(days=GRACE_DAYS + 1))

        assert judge(late, "MODERATE", [], TODAY).status == "blocks"

    def test_a_low_finding_on_the_last_day_still_does_not_block(self) -> None:
        edge = finding(published=TODAY - timedelta(days=GRACE_DAYS))

        assert judge(edge, "LOW", [], TODAY).status == "reported"

    def test_an_accepted_risk_lets_even_a_critical_finding_through(self) -> None:
        judgement = judge(finding(), "CRITICAL", [acceptance()], TODAY)

        assert judgement.status == "accepted"
        assert "Ruslan" in judgement.explanation

    def test_an_acceptance_for_another_package_does_not_help(self) -> None:
        other = acceptance(package="requests")

        assert judge(finding(), "CRITICAL", [other], TODAY).status == "blocks"


class TestStaleAcceptances:
    def test_an_acceptance_nobody_reports_any_more_is_a_problem(self) -> None:
        """The dependency was upgraded, the hole is gone, and the entry stays
        behind telling the next reader that the repository still has it."""
        assert unused_acceptances([acceptance()], []) == [acceptance()]

    def test_an_acceptance_that_matches_a_finding_is_used(self) -> None:
        assert unused_acceptances([acceptance()], [finding()]) == []
