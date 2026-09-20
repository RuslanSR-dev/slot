"""Unit level: the verdict and the quarantine policy, with time as a parameter.

A history is written as a string of outcomes, oldest run first: `P` passed,
`F` failed, `E` errored, `S` skipped. Unless commits are given, every run sits
on its own commit — repeating a commit is how a retry is expressed.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quality_hub.flakiness import (
    DEFAULT_POLICY,
    CaseRun,
    FlakinessPolicy,
    FlakyVerdict,
    QuarantineEntry,
    QuarantineState,
    classify,
    earned_release,
    expires_at,
    flip_rate,
    flipped_on_same_commit,
    is_expired,
    quarantine_state,
    quarantined_ids,
    runs_from_report,
    should_quarantine,
    window_of,
)
from quality_hub.junit import CaseStatus, parse_report_file

# A real pytest report with a pass, a failure, a setup error and a skip in it.
MIXED_REPORT = Path(__file__).parents[1] / "fixtures" / "junit-mixed.xml"

TEST_ID = "tests.unit.test_domain.TestParsing::test_a_broken_date_is_not_an_event"
OPENED_AT = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

STATUS_BY_LETTER = {
    "P": CaseStatus.PASSED,
    "F": CaseStatus.FAILED,
    "E": CaseStatus.ERROR,
    "S": CaseStatus.SKIPPED,
}


def history(outcomes: str, commits: list[str] | None = None) -> list[CaseRun]:
    names = commits if commits is not None else [f"c{number}" for number in range(len(outcomes))]
    seen: dict[str, int] = {}
    runs = []
    for letter, commit in zip(outcomes, names, strict=True):
        seen[commit] = seen.get(commit, 0) + 1
        runs.append(
            CaseRun(
                test_id=TEST_ID,
                commit=commit,
                branch="main",
                attempt=seen[commit],
                status=STATUS_BY_LETTER[letter],
                duration=0.25,
            )
        )
    return runs


class TestPolicyDefaults:
    """The thresholds live in one object, so one test pins all of them.

    Without this, changing a default is a silent change of the gate.
    """

    def test_every_threshold_has_the_value_adr_0015_argues_for(self) -> None:
        assert DEFAULT_POLICY.window == 20
        assert DEFAULT_POLICY.min_runs == 5
        assert DEFAULT_POLICY.flip_rate_threshold == 0.15
        assert DEFAULT_POLICY.quarantine_ttl == timedelta(days=14)
        assert DEFAULT_POLICY.stable_runs_to_release == 20

    def test_leaving_quarantine_costs_more_evidence_than_entering_it(self) -> None:
        assert DEFAULT_POLICY.stable_runs_to_release >= DEFAULT_POLICY.min_runs

    def test_an_entry_is_flaky_unless_it_says_otherwise(self) -> None:
        entry = QuarantineEntry(test_id=TEST_ID, owner="aqa-team", opened_at=OPENED_AT)

        assert entry.reason == FlakyVerdict.FLAKY


class TestTheSameCommitFailedAndPassed:
    def test_a_retry_that_passed_on_the_same_commit_is_the_signal(self) -> None:
        """The code did not change between the attempts, so the test is the variable."""
        assert flipped_on_same_commit(history("FP", ["c1", "c1"])) is True

    def test_an_error_counts_as_not_passing(self) -> None:
        assert flipped_on_same_commit(history("EP", ["c1", "c1"])) is True

    def test_one_flipping_commit_among_calm_ones_is_enough(self) -> None:
        assert flipped_on_same_commit(history("FPP", ["c1", "c1", "c2"])) is True

    def test_the_same_result_twice_on_one_commit_is_not_a_flip(self) -> None:
        assert flipped_on_same_commit(history("FF", ["c1", "c1"])) is False

    def test_different_commits_disagreeing_is_not_this_signal(self) -> None:
        """Between two commits the code changed; that is a regression, not flakiness."""
        assert flipped_on_same_commit(history("PF")) is False

    def test_a_skipped_run_next_to_a_failure_is_not_a_flip(self) -> None:
        assert flipped_on_same_commit(history("SF", ["c1", "c1"])) is False

    def test_the_proof_outranks_the_minimum_number_of_runs(self) -> None:
        """Two runs are below `min_runs` and still enough when the commit is the same."""
        assert classify(history("FP", ["c1", "c1"])) == FlakyVerdict.FLAKY


class TestWindow:
    def test_only_the_last_n_runs_are_looked_at(self) -> None:
        runs = history("P" * 25)

        assert len(window_of(runs)) == DEFAULT_POLICY.window
        assert window_of(runs) == tuple(runs[-DEFAULT_POLICY.window :])

    def test_a_history_shorter_than_the_window_is_used_whole(self) -> None:
        runs = history("PFP")

        assert window_of(runs) == tuple(runs)

    def test_skipped_runs_are_dropped_before_anything_is_counted(self) -> None:
        """A skipped test ran no assertions: it is evidence of nothing."""
        assert [run.status for run in window_of(history("PSF"))] == [
            CaseStatus.PASSED,
            CaseStatus.FAILED,
        ]


class TestFlipRate:
    @pytest.mark.parametrize(
        ("outcomes", "expected"),
        [
            ("PF", 1.0),
            ("PPPP", 0.0),
            ("FFFF", 0.0),
            ("PPFFP", 0.5),
            ("PFPFP", 1.0),
            ("PPPPF", 0.25),
        ],
    )
    def test_it_is_the_share_of_adjacent_pairs_that_changed(
        self, outcomes: str, expected: float
    ) -> None:
        assert flip_rate(history(outcomes)) == pytest.approx(expected)

    def test_a_test_seen_for_the_first_time_has_no_rate(self) -> None:
        """No pairs, so no rate — and nothing that can cross a threshold."""
        assert flip_rate(history("P")) == 0.0

    def test_no_runs_at_all_have_no_rate(self) -> None:
        assert flip_rate([]) == 0.0

    def test_an_error_and_a_failure_are_the_same_side_of_a_flip(self) -> None:
        assert flip_rate(history("FE")) == 0.0

    def test_skipped_runs_do_not_break_a_streak(self) -> None:
        assert flip_rate(history("PSP")) == 0.0


class TestVerdict:
    def test_a_test_seen_once_is_not_judged(self) -> None:
        assert classify(history("F")) == FlakyVerdict.NOT_ENOUGH_DATA

    def test_one_run_short_of_the_minimum_is_still_not_judged(self) -> None:
        assert classify(history("P" * (DEFAULT_POLICY.min_runs - 1))) == (
            FlakyVerdict.NOT_ENOUGH_DATA
        )

    def test_exactly_the_minimum_number_of_runs_gives_a_verdict(self) -> None:
        assert classify(history("P" * DEFAULT_POLICY.min_runs)) == FlakyVerdict.STABLE

    def test_a_test_that_always_fails_is_broken_and_not_flaky(self) -> None:
        """Quarantining it would silence a real defect."""
        assert classify(history("F" * 8)) == FlakyVerdict.BROKEN

    def test_a_test_that_always_errors_is_broken_too(self) -> None:
        assert classify(history("E" * 8)) == FlakyVerdict.BROKEN

    def test_a_test_that_broke_and_stayed_broken_is_not_flaky_either(self) -> None:
        """One flip in a long window is a regression with a date, not noise."""
        assert classify(history("PPPPPPPPPPFFFFFFFFFF")) == FlakyVerdict.STABLE

    def test_a_history_full_of_flips_is_flaky(self) -> None:
        assert classify(history("PFPFPFPF")) == FlakyVerdict.FLAKY

    def test_a_mostly_failing_history_with_one_pass_is_not_called_broken(self) -> None:
        assert classify(history("FFFFFP")) == FlakyVerdict.FLAKY

    def test_exactly_at_the_threshold_the_test_is_flaky(self) -> None:
        """The boundary belongs to the flaky side: the threshold is `>=`."""
        policy = FlakinessPolicy(window=5, min_runs=2, flip_rate_threshold=0.5)

        assert flip_rate(history("PPFFP"), policy) == pytest.approx(0.5)
        assert classify(history("PPFFP"), policy) == FlakyVerdict.FLAKY

    def test_just_under_the_threshold_the_test_is_stable(self) -> None:
        policy = FlakinessPolicy(window=5, min_runs=2, flip_rate_threshold=0.51)

        assert classify(history("PPFFP"), policy) == FlakyVerdict.STABLE


class TestWhoGoesIntoQuarantine:
    def test_a_flaky_test_is_quarantined(self) -> None:
        assert should_quarantine(history("PFPFPFPF")) is True

    def test_a_broken_test_is_not_quarantined(self) -> None:
        assert should_quarantine(history("F" * 8)) is False

    def test_a_stable_test_is_not_quarantined(self) -> None:
        assert should_quarantine(history("P" * 8)) is False

    def test_a_test_we_have_barely_seen_is_not_quarantined(self) -> None:
        assert should_quarantine(history("PF")) is False


class TestQuarantineLifetime:
    entry = QuarantineEntry(test_id=TEST_ID, owner="aqa-team", opened_at=OPENED_AT)

    def test_the_deadline_is_the_opening_plus_the_time_to_live(self) -> None:
        assert expires_at(self.entry) == OPENED_AT + DEFAULT_POLICY.quarantine_ttl

    def test_a_second_before_the_deadline_the_entry_still_holds(self) -> None:
        assert is_expired(self.entry, expires_at(self.entry) - timedelta(seconds=1)) is False

    def test_at_the_deadline_the_entry_has_expired(self) -> None:
        """Inclusive on purpose: 'exactly on time' is the same as late."""
        assert is_expired(self.entry, expires_at(self.entry)) is True

    def test_an_entry_nobody_fixed_expires_and_the_build_goes_red_again(self) -> None:
        state = quarantine_state(self.entry, history("PFPFPF"), OPENED_AT + timedelta(days=30))

        assert state == QuarantineState.EXPIRED

    def test_a_fresh_entry_is_active(self) -> None:
        state = quarantine_state(self.entry, history("PFPFPF"), OPENED_AT + timedelta(days=1))

        assert state == QuarantineState.ACTIVE


class TestLeavingQuarantine:
    entry = QuarantineEntry(test_id=TEST_ID, owner="aqa-team", opened_at=OPENED_AT)

    def test_exactly_the_required_number_of_clean_runs_releases_the_test(self) -> None:
        clean = history("P" * DEFAULT_POLICY.stable_runs_to_release)

        assert earned_release(clean) is True

    def test_one_clean_run_short_is_not_enough(self) -> None:
        clean = history("P" * (DEFAULT_POLICY.stable_runs_to_release - 1))

        assert earned_release(clean) is False

    def test_the_streak_is_counted_from_the_end(self) -> None:
        """The failure that opened the entry is history; what follows it is the evidence."""
        assert earned_release(history("F" + "P" * DEFAULT_POLICY.stable_runs_to_release)) is True

    def test_a_failure_inside_the_streak_resets_it(self) -> None:
        outcomes = "F" + "P" * (DEFAULT_POLICY.stable_runs_to_release - 1)

        assert earned_release(history(outcomes)) is False

    def test_skipped_runs_do_not_count_towards_the_streak(self) -> None:
        """Otherwise a quarantined test could be released by never running."""
        outcomes = "P" * (DEFAULT_POLICY.stable_runs_to_release - 1) + "S"

        assert earned_release(history(outcomes)) is False

    def test_a_released_test_leaves_quarantine_on_its_own(self) -> None:
        clean = history("P" * DEFAULT_POLICY.stable_runs_to_release)

        state = quarantine_state(self.entry, clean, OPENED_AT + timedelta(days=1))

        assert state == QuarantineState.RELEASED

    def test_healing_on_the_last_day_releases_instead_of_expiring(self) -> None:
        """Failing the build for a test that did exactly what we asked would punish the fix."""
        clean = history("P" * DEFAULT_POLICY.stable_runs_to_release)

        state = quarantine_state(self.entry, clean, OPENED_AT + timedelta(days=30))

        assert state == QuarantineState.RELEASED


class TestTheListGivenToThePytestPlugin:
    def test_only_active_entries_are_handed_out_and_they_are_sorted(self) -> None:
        entries = [
            QuarantineEntry(test_id="b::t", owner="aqa-team", opened_at=OPENED_AT),
            QuarantineEntry(test_id="a::t", owner="aqa-team", opened_at=OPENED_AT),
            QuarantineEntry(test_id="c::t", owner="aqa-team", opened_at=OPENED_AT),
        ]
        states = {
            "a::t": QuarantineState.ACTIVE,
            "b::t": QuarantineState.EXPIRED,
            "c::t": QuarantineState.ACTIVE,
        }

        assert quarantined_ids(entries, states) == ("a::t", "c::t")

    def test_an_entry_without_a_known_state_is_not_handed_out(self) -> None:
        entries = [QuarantineEntry(test_id="a::t", owner="aqa-team", opened_at=OPENED_AT)]

        assert quarantined_ids(entries, {}) == ()


class TestRunsFromAReport:
    def test_a_report_becomes_runs_that_carry_the_ci_metadata(self) -> None:
        runs = runs_from_report(
            parse_report_file(MIXED_REPORT), commit="deadbee", branch="main", attempt=2
        )

        assert len(runs) == 6
        assert {run.commit for run in runs} == {"deadbee"}
        assert {run.branch for run in runs} == {"main"}
        assert {run.attempt for run in runs} == {2}

    def test_a_run_knows_which_test_it_belongs_to(self) -> None:
        runs = runs_from_report(
            parse_report_file(MIXED_REPORT), commit="deadbee", branch="main", attempt=1
        )

        assert "tests.unit.test_failures.TestQuarantineDemo::test_a_stable_test_passes" in {
            run.test_id for run in runs
        }

    def test_the_duration_survives_the_conversion(self) -> None:
        """The slowest tests come from the same upload; losing the number loses the report."""
        report = parse_report_file(MIXED_REPORT)
        runs = runs_from_report(report, commit="deadbee", branch="main", attempt=1)
        cases = {case.test_id: case.duration for suite in report for case in suite.cases}

        assert {run.test_id: run.duration for run in runs} == cases

    def test_the_statuses_survive_the_conversion(self) -> None:
        runs = runs_from_report(
            parse_report_file(MIXED_REPORT), commit="deadbee", branch="main", attempt=1
        )
        statuses = sorted(run.status for run in runs)

        assert statuses == [
            CaseStatus.ERROR,
            CaseStatus.FAILED,
            CaseStatus.FAILED,
            CaseStatus.PASSED,
            CaseStatus.PASSED,
            CaseStatus.SKIPPED,
        ]

    def test_two_attempts_on_one_commit_make_a_history_the_verdict_can_read(self) -> None:
        """This is how the strongest signal reaches the hub: the CI step retries."""
        report = parse_report_file(MIXED_REPORT)
        first = runs_from_report(report, commit="deadbee", branch="main", attempt=1)
        broken = next(run for run in first if run.status == CaseStatus.FAILED)
        retried = CaseRun(
            test_id=broken.test_id,
            commit="deadbee",
            branch="main",
            attempt=2,
            status=CaseStatus.PASSED,
            duration=0.1,
        )

        assert classify([broken, retried]) == FlakyVerdict.FLAKY


class TestTheThresholdsAreReallyTakenFromThePolicy:
    """Every entry point must pass the policy on, not quietly fall back to the default.

    Mutation testing found this: nine mutants that deleted the `policy`
    argument survived, because every test used the defaults. A policy object
    nobody threads through is a settings screen that changes nothing.
    """

    entry = QuarantineEntry(test_id=TEST_ID, owner="aqa-team", opened_at=OPENED_AT)
    # Ten noisy runs, then twenty calm ones: a window of 30 sees the noise, the
    # default window of 20 has already scrolled past it.
    noisy_then_calm = "PFPFPFPFPF" + "P" * 20
    wide = FlakinessPolicy(window=30)

    def test_a_narrow_window_hides_older_flips(self) -> None:
        assert flip_rate(history("PFPFPFPPPP"), FlakinessPolicy(window=3)) == 0.0
        assert flip_rate(history("PFPFPFPPPP")) > 0.0

    def test_a_wide_window_sees_flips_the_default_one_has_scrolled_past(self) -> None:
        runs = history(self.noisy_then_calm)

        assert flip_rate(runs, self.wide) > self.wide.flip_rate_threshold
        assert flip_rate(runs) == 0.0

    def test_the_verdict_follows_the_window_it_was_given(self) -> None:
        runs = history(self.noisy_then_calm)

        assert classify(runs, self.wide) == FlakyVerdict.FLAKY
        assert classify(runs) == FlakyVerdict.STABLE

    def test_quarantine_follows_the_window_it_was_given(self) -> None:
        runs = history(self.noisy_then_calm)

        assert should_quarantine(runs, self.wide) is True
        assert should_quarantine(runs) is False

    def test_expiry_follows_the_time_to_live_it_was_given(self) -> None:
        short = FlakinessPolicy(quarantine_ttl=timedelta(hours=1))
        a_day_later = OPENED_AT + timedelta(days=1)

        assert is_expired(self.entry, a_day_later, short) is True
        assert is_expired(self.entry, a_day_later) is False

    def test_release_follows_the_number_of_clean_runs_it_was_given(self) -> None:
        lenient = FlakinessPolicy(stable_runs_to_release=3)
        clean = history("PPP")

        assert quarantine_state(self.entry, clean, OPENED_AT, lenient) == QuarantineState.RELEASED
        assert quarantine_state(self.entry, clean, OPENED_AT) == QuarantineState.ACTIVE

    def test_the_state_follows_the_time_to_live_it_was_given(self) -> None:
        short = FlakinessPolicy(quarantine_ttl=timedelta(hours=1))
        a_day_later = OPENED_AT + timedelta(days=1)
        noisy = history("PFPFPF")

        assert quarantine_state(self.entry, noisy, a_day_later, short) == QuarantineState.EXPIRED
        assert quarantine_state(self.entry, noisy, a_day_later) == QuarantineState.ACTIVE
