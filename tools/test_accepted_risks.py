"""The list of accepted risks is the part of the policy that actually decays.

A scanner that finds nothing tells us nothing; an ignore that outlived its
reason is the thing that hurts. So the interesting tests here are the ones
that prove an entry can no longer be quiet: an expired date, a deadline set to
the next decade, an owner that is missing, a reason that says nothing.
"""

from datetime import date, datetime, timedelta
from typing import Any

from accepted_risks import MAX_DAYS, MAX_DAYS_WITHOUT_FIX, AcceptedRisk, parse

TODAY = date(2026, 9, 20)
REASON = (
    "Reachable only from the build step, which never reads data from outside"
    " the repository; the fix needs a major upgrade planned for iteration 7."
)


def entry(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": "GHSA-0000-0000-0000",
        "package": "example",
        "reason": REASON,
        "owner": "Ruslan",
        "added": date(2026, 9, 1),
        "until": date(2026, 11, 1),
        "link": "https://github.com/advisories/GHSA-0000-0000-0000",
    }
    raw.update(overrides)
    return raw


def document(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"accepted": list(entries)}


def problems_of(*entries: dict[str, Any], today: date = TODAY) -> list[str]:
    return parse(document(*entries), today)[1]


def test_an_empty_list_is_the_normal_state() -> None:
    risks, problems = parse({}, TODAY)

    assert (risks, problems) == ([], [])


def test_a_complete_entry_is_accepted() -> None:
    risks, problems = parse(document(entry()), TODAY)

    assert problems == []
    assert [risk.id for risk in risks] == ["GHSA-0000-0000-0000"]


def test_an_expired_entry_fails_the_build() -> None:
    """The whole point: a decision that nobody renewed stops being quiet."""
    [problem] = problems_of(entry(until=date(2026, 9, 19)))

    assert "expired on 2026-09-19" in problem


def test_an_entry_that_expires_today_is_still_valid() -> None:
    assert problems_of(entry(until=TODAY)) == []


def test_acceptance_cannot_be_longer_than_the_limit() -> None:
    [problem] = problems_of(entry(until=date(2026, 9, 1) + timedelta(days=MAX_DAYS + 1)))

    assert f"the limit is {MAX_DAYS}" in problem


def test_without_a_fix_the_deadline_is_shorter() -> None:
    """No fix exists, so the only question to re-ask is whether one appeared."""
    [problem] = problems_of(entry(no_fix=True, until=date(2026, 10, 15)))

    assert f"the limit is {MAX_DAYS_WITHOUT_FIX} because no fix exists yet" in problem


def test_a_deadline_before_the_decision_is_rejected() -> None:
    [problem] = problems_of(entry(added=date(2026, 9, 1), until=date(2026, 8, 1)))

    assert "is not after added" in problem


def test_a_reason_that_says_nothing_is_rejected() -> None:
    [problem] = problems_of(entry(reason="temporarily"))

    assert "say what the risk is" in problem


def test_a_missing_owner_is_rejected() -> None:
    """A team renews nothing; a person does."""
    [problem] = problems_of(entry(owner=" "))

    assert "field 'owner' must be a non-empty string" in problem


def test_an_unknown_field_is_rejected() -> None:
    """A typo in `until` would otherwise become an acceptance with no deadline."""
    [problem] = problems_of(entry(untill=date(2027, 1, 1)))

    assert "unknown field 'untill'" in problem


def test_a_missing_deadline_is_rejected() -> None:
    raw = entry()
    del raw["until"]

    [problem] = problems_of(raw)

    assert "field 'until' must be a date" in problem


def test_a_timestamp_is_not_a_deadline() -> None:
    [problem] = problems_of(entry(until=datetime(2026, 11, 1, 12, 0)))

    assert "field 'until' must be a date" in problem


def test_a_link_that_is_not_an_advisory_is_rejected() -> None:
    [problem] = problems_of(entry(link="internal wiki"))

    assert "must be an https:// address" in problem


def test_the_same_advisory_cannot_be_accepted_twice() -> None:
    """Two entries mean two deadlines, and the later one wins by accident."""
    [problem] = problems_of(entry(), entry(until=date(2026, 11, 20)))

    assert "accepted twice" in problem


def test_every_problem_is_reported_at_once() -> None:
    problems = problems_of(entry(reason="no", link="wiki", until=date(2026, 9, 2)))

    assert len(problems) == 3


def test_an_acceptance_covers_one_package_only() -> None:
    """The same advisory in a service is not the same decision as in a tool."""
    risk = AcceptedRisk(
        id="CVE-2026-1",
        package="example",
        reason=REASON,
        owner="Ruslan",
        added=date(2026, 9, 1),
        until=date(2026, 11, 1),
        link="https://example.invalid",
        no_fix=False,
    )

    assert risk.covers("example", frozenset({"GHSA-x", "CVE-2026-1"}))
    assert not risk.covers("other", frozenset({"CVE-2026-1"}))
    assert not risk.covers("example", frozenset({"CVE-2026-2"}))
