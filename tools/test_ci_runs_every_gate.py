"""The pipeline must run every gate the Makefile calls mandatory.

`make check` is the list of what a change has to pass; the workflow runs the
same targets one by one, so that a red build says which gate failed instead
of just "check failed". The price of that is drift: a gate added to `check`
and forgotten in the workflow exists only on the author's machine.

That is exactly what happened with the event schema gates of iteration 3:
they were in `check` for two days and in no pipeline at all. A gate that
never runs proves nothing, so the drift itself is now a gate.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = REPO_ROOT / "Makefile"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def mandatory_targets() -> list[str]:
    """The prerequisites of `check`: what the Makefile says a change must pass."""
    line = next(line for line in MAKEFILE.read_text().splitlines() if line.startswith("check:"))
    targets = line.split("##")[0].removeprefix("check:").split()
    return [target for target in targets if target]


def targets_run_in_ci() -> set[str]:
    """Every `make <target>` the workflow runs, in any job."""
    return set(re.findall(r"run: make ([a-z-]+)", WORKFLOW.read_text()))


def test_every_mandatory_gate_runs_in_the_pipeline() -> None:
    missing = [target for target in mandatory_targets() if target not in targets_run_in_ci()]

    assert missing == [], f"in `make check` but never run by CI: {missing}"


def test_check_is_not_empty() -> None:
    """A green check over nothing is the failure mode this file is about."""
    assert len(mandatory_targets()) >= 5
