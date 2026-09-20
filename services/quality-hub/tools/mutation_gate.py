"""Fail the build when the mutation score of the domain module drops.

Coverage says which lines ran; the mutation score says whether the tests
would notice if those lines were wrong. Threshold is a ratchet: raise only.
"""

import json
import sys
from pathlib import Path

THRESHOLD = 1.0
STATS = Path("mutants/mutmut-cicd-stats.json")
NOT_KILLED = ("survived", "no_tests", "timeout", "suspicious", "skipped", "segfault")


def main() -> int:
    if not STATS.exists():
        print(f"{STATS} is missing: mutmut did not finish, see reports/mutmut-run.log")
        return 1
    stats = json.loads(STATS.read_text())
    total, killed = stats["total"], stats["killed"]
    if total == 0:
        print("no mutants were generated: a gate over nothing proves nothing")
        return 1

    score = killed / total
    print(f"mutation score: {killed}/{total} = {score:.0%} (threshold {THRESHOLD:.0%})")
    if score < THRESHOLD:
        missed = {key: stats[key] for key in NOT_KILLED if stats.get(key)}
        print(f"not killed: {missed}")
        print("inspect with: uv run mutmut results, then uv run mutmut show <mutant>")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
