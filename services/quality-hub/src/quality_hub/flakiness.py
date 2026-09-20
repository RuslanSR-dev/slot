"""Is this test flaky, and what do we do with it: pure rules, no database.

A flaky test is one whose result does not depend on the code under test. The
damage is not the red build, it is what a team learns from it: "red means
press retry". After that a real defect gets retried too.

Two signals are used here, and they are not equal in strength:

1. **The same commit both failed and passed.** The code did not change between
   the attempts, so the test is the only variable left. This is proof, not a
   guess, and it needs no window — two runs are enough.
2. **Flip rate in a window of the last N runs.** A statistic, not proof: a
   test that broke on Monday and was fixed on Tuesday also flips. It is the
   fallback for tests nobody ever retried, and it needs a minimum number of
   runs before it is allowed to say anything.

The third case matters as much as the first two: a test that fails *every*
time is not flaky, it is broken. Quarantining it would silence a real defect,
so it gets its own verdict and never enters quarantine.

Time never comes from the clock inside these functions: `now` is a parameter
(rule 6 of docs/test-strategy.md). Every threshold lives in `FlakinessPolicy`,
so changing the policy is one object, not a search through the code.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from quality_hub.junit import CaseStatus, SuiteResult, cases_of

# Statuses that say something about the code. A skipped test ran no assertions,
# so it is neither evidence of health nor of flakiness and is dropped before
# anything is counted.
CONCLUSIVE_STATUSES = (CaseStatus.PASSED, CaseStatus.FAILED, CaseStatus.ERROR)


class FlakyVerdict(StrEnum):
    # Too few conclusive runs to say anything. A test seen once lands here.
    NOT_ENOUGH_DATA = "not_enough_data"
    STABLE = "stable"
    # Fails every time in the window: a defect, not flakiness. Never quarantined.
    BROKEN = "broken"
    FLAKY = "flaky"


class QuarantineState(StrEnum):
    # Runs, does not block the build.
    ACTIVE = "active"
    # Proved itself stable again and left quarantine on its own.
    RELEASED = "released"
    # Nobody fixed it in time. From here the build is red again, by design.
    EXPIRED = "expired"


@dataclass(frozen=True)
class FlakinessPolicy:
    """Every threshold in one place. Defaults are explained in ADR-0015.

    They are deliberately conservative: a false "flaky" verdict hides a real
    defect, which is a worse outcome than a flaky test we noticed a week late.
    """

    # How many recent runs the statistic looks at. Twenty runs is about a week
    # of a pipeline that runs on every push.
    window: int = 20
    # Below this, the flip rate is noise. Four runs give flip rates of 0, 0.33,
    # 0.67 and 1.0 — nothing in between, so no threshold is meaningful there.
    min_runs: int = 5
    # 15% of adjacent pairs flipping. One flip in twenty runs (0.05) is a
    # deploy or a real fix; three (0.16) is not.
    flip_rate_threshold: float = 0.15
    # An entry is not a parking place: two weeks is one sprint plus slack.
    quarantine_ttl: timedelta = timedelta(days=14)
    # Leaving quarantine costs more evidence than entering it, because a wrong
    # release puts the noise back into everyone's build.
    stable_runs_to_release: int = 20


DEFAULT_POLICY = FlakinessPolicy()


@dataclass(frozen=True)
class CaseRun:
    """One execution of one test, with the context that makes it comparable."""

    test_id: str
    commit: str
    branch: str
    # 1 is the first execution in the pipeline; 2 and up are retries.
    attempt: int
    status: CaseStatus
    # Seconds.
    duration: float


@dataclass(frozen=True)
class QuarantineEntry:
    """A test excused from blocking the build — with a name on it and a deadline.

    `owner` is not decoration: an entry without an owner is how a quarantine
    list turns into a graveyard of tests nobody will ever look at.
    """

    test_id: str
    owner: str
    opened_at: datetime
    reason: FlakyVerdict = FlakyVerdict.FLAKY


def runs_from_report(
    suites: Iterable[SuiteResult],
    *,
    commit: str,
    branch: str,
    attempt: int,
) -> tuple[CaseRun, ...]:
    """Turn one uploaded report into runs. The metadata comes from the CI step."""
    return tuple(
        CaseRun(
            test_id=case.test_id,
            commit=commit,
            branch=branch,
            attempt=attempt,
            status=case.status,
            duration=case.duration,
        )
        for case in cases_of(tuple(suites))
    )


def conclusive(runs: Sequence[CaseRun]) -> tuple[CaseRun, ...]:
    """Drop the runs that carry no signal, keeping the original order."""
    return tuple(run for run in runs if run.status in CONCLUSIVE_STATUSES)


def failed(run: CaseRun) -> bool:
    """A run that did not pass. An `error` counts: the test did not do its job."""
    return run.status != CaseStatus.PASSED


def flipped_on_same_commit(runs: Sequence[CaseRun]) -> bool:
    """Did any single commit produce both a pass and a non-pass?

    The strongest signal we have: the code was identical between the attempts.
    """
    outcomes: dict[str, set[bool]] = {}
    for run in conclusive(runs):
        outcomes.setdefault(run.commit, set()).add(failed(run))
    return any(len(seen) > 1 for seen in outcomes.values())


def window_of(
    runs: Sequence[CaseRun], policy: FlakinessPolicy = DEFAULT_POLICY
) -> tuple[CaseRun, ...]:
    """The last `window` conclusive runs, oldest first.

    Callers pass runs in chronological order; the hub stores them that way.
    A history shorter than the window is used as it is — it is not padded.
    """
    return conclusive(runs)[-policy.window :]


def flip_rate(runs: Sequence[CaseRun], policy: FlakinessPolicy = DEFAULT_POLICY) -> float:
    """Share of adjacent pairs in the window where the outcome changed.

    Fewer than two conclusive runs means no pairs and therefore no rate: 0.0,
    which never crosses a threshold. That is the "first time we see this test"
    case, and it must not be reported as flaky.
    """
    recent = window_of(runs, policy)
    if len(recent) < 2:
        return 0.0
    flips = sum(1 for before, after in pairwise(recent) if failed(before) != failed(after))
    return flips / (len(recent) - 1)


def classify(runs: Sequence[CaseRun], policy: FlakinessPolicy = DEFAULT_POLICY) -> FlakyVerdict:
    """The verdict for one test from its history, newest run last."""
    recent = window_of(runs, policy)
    # Proof beats statistics: a retry that passed on the same commit is enough
    # on its own, so it is checked before the minimum-runs rule.
    if flipped_on_same_commit(recent):
        return FlakyVerdict.FLAKY
    if len(recent) < policy.min_runs:
        return FlakyVerdict.NOT_ENOUGH_DATA
    if all(failed(run) for run in recent):
        return FlakyVerdict.BROKEN
    if flip_rate(recent, policy) >= policy.flip_rate_threshold:
        return FlakyVerdict.FLAKY
    return FlakyVerdict.STABLE


def should_quarantine(runs: Sequence[CaseRun], policy: FlakinessPolicy = DEFAULT_POLICY) -> bool:
    """Only a flaky verdict earns quarantine. A broken test must stay red."""
    return classify(runs, policy) == FlakyVerdict.FLAKY


def expires_at(entry: QuarantineEntry, policy: FlakinessPolicy = DEFAULT_POLICY) -> datetime:
    return entry.opened_at + policy.quarantine_ttl


def is_expired(
    entry: QuarantineEntry, now: datetime, policy: FlakinessPolicy = DEFAULT_POLICY
) -> bool:
    """At the deadline exactly, the entry is already expired: the clock is inclusive."""
    return now >= expires_at(entry, policy)


def earned_release(runs_since: Sequence[CaseRun], policy: FlakinessPolicy = DEFAULT_POLICY) -> bool:
    """Has the test passed `stable_runs_to_release` conclusive runs in a row since quarantine?"""
    recent = conclusive(runs_since)
    if len(recent) < policy.stable_runs_to_release:
        return False
    return not any(failed(run) for run in recent[-policy.stable_runs_to_release :])


def quarantine_state(
    entry: QuarantineEntry,
    runs_since: Sequence[CaseRun],
    now: datetime,
    policy: FlakinessPolicy = DEFAULT_POLICY,
) -> QuarantineState:
    """Where the entry stands right now.

    Release is checked before expiry on purpose: a test that healed on the last
    day of its quarantine has done what we asked, and failing the build for it
    would punish the fix.
    """
    if earned_release(runs_since, policy):
        return QuarantineState.RELEASED
    if is_expired(entry, now, policy):
        return QuarantineState.EXPIRED
    return QuarantineState.ACTIVE


def quarantined_ids(
    entries: Iterable[QuarantineEntry],
    states: dict[str, QuarantineState],
) -> tuple[str, ...]:
    """The list handed to the pytest plugin: active entries only, sorted.

    An expired entry is deliberately *not* on it — that is how an overdue
    quarantine turns the build red again.
    """
    return tuple(
        sorted(
            entry.test_id
            for entry in entries
            if states.get(entry.test_id) == QuarantineState.ACTIVE
        )
    )
