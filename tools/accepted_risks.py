"""Gate: the list of accepted security risks is valid and none of it has expired.

    python tools/accepted_risks.py [FILE]   ->  exit 1 on an invalid or expired entry

A scanner is the cheap half of a vulnerability policy. The expensive half is
what happens to a finding nobody can fix today. If the answer is a flag in the
pipeline (`--ignore GHSA-...`), the hole disappears from sight: the flag has no
author, no reason and no end date, and a year later nobody knows whether it is
still needed - or what it hides.

So an accepted risk is a record in the repository, reviewed like code, and it
rots on purpose: when `until` passes, this check goes red and somebody has to
look at the entry again. A risk accepted until a date is a decision; the same
risk accepted forever is a forgotten hole, and only a red build tells them
apart.

The limits below are the mechanical part of ADR-0018:

- a reason long enough that "temporarily" does not fit into it;
- an owner, because "the team" never renews anything;
- a deadline, capped - and capped shorter when no fix exists at all, because
  then the only question worth re-asking is "has a fix appeared yet?".
"""

import sys
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = REPO_ROOT / "security" / "accepted-risks.toml"

# How long a risk may be accepted. Three months is enough to plan a major
# upgrade and short enough that the person who wrote the entry is still around.
MAX_DAYS = 90
# No fix exists yet: the only useful question is "is there a fix now?", and
# that is worth asking monthly, not quarterly.
MAX_DAYS_WITHOUT_FIX = 30
# A reason shorter than this is a label, not an argument.
MIN_REASON_CHARS = 60

TEXT_FIELDS = ("id", "package", "reason", "owner", "link")
DATE_FIELDS = ("added", "until")
KNOWN_FIELDS = frozenset({*TEXT_FIELDS, *DATE_FIELDS, "no_fix"})


@dataclass(frozen=True)
class AcceptedRisk:
    """One vulnerability somebody decided to ship with, and until when."""

    id: str
    package: str
    reason: str
    owner: str
    added: date
    until: date
    link: str
    no_fix: bool

    def covers(self, package: str, identifiers: frozenset[str]) -> bool:
        """The entry silences one advisory in one package: a risk accepted in a
        test-only dependency must not silence the same advisory in a service."""
        return self.package == package and self.id in identifiers


def is_plain_date(value: Any) -> bool:
    """TOML has both dates and timestamps; only a plain date is a deadline."""
    return isinstance(value, date) and not isinstance(value, datetime)


def field_problems(raw: dict[str, Any]) -> list[str]:
    """Shape of one entry: every field present, of the right type, none extra.

    Unknown fields are an error on purpose - a typo in `until` would otherwise
    turn into an acceptance with no deadline at all.
    """
    problems = [f"unknown field '{name}'" for name in sorted(set(raw) - KNOWN_FIELDS)]
    for name in TEXT_FIELDS:
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"field '{name}' must be a non-empty string")
    for name in DATE_FIELDS:
        if not is_plain_date(raw.get(name)):
            problems.append(f"field '{name}' must be a date, for example 2026-12-31")
    if not isinstance(raw.get("no_fix", False), bool):
        problems.append("field 'no_fix' must be true or false")
    return problems


def content_problems(risk: AcceptedRisk, today: date) -> list[str]:
    """What ADR-0018 allows an acceptance to say."""
    problems: list[str] = []
    reason = risk.reason.strip()
    if len(reason) < MIN_REASON_CHARS:
        problems.append(
            f"reason is {len(reason)} characters, at least {MIN_REASON_CHARS} are"
            " required: say what the risk is and what limits it"
        )
    if not risk.link.startswith("https://"):
        problems.append(f"link '{risk.link}' must be an https:// address of the advisory")
    if risk.until <= risk.added:
        problems.append(f"until {risk.until} is not after added {risk.added}")
        return problems
    limit = MAX_DAYS_WITHOUT_FIX if risk.no_fix else MAX_DAYS
    days = (risk.until - risk.added).days
    if days > limit:
        reason_for_limit = " because no fix exists yet" if risk.no_fix else ""
        problems.append(f"accepted for {days} days, the limit is {limit}{reason_for_limit}")
    if risk.until < today:
        problems.append(f"expired on {risk.until}: fix it, or renew the decision with a new reason")
    return problems


def parse(document: dict[str, Any], today: date) -> tuple[list[AcceptedRisk], list[str]]:
    """Read the whole file and report every problem at once: somebody fixing
    this file should not have to run the gate five times to see five typos."""
    entries = document.get("accepted", [])
    if not isinstance(entries, list):
        return [], ["'accepted' must be a list of [[accepted]] tables"]
    risks: list[AcceptedRisk] = []
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for position, raw in enumerate(entries, start=1):
        if not isinstance(raw, dict):
            problems.append(f"entry {position}: must be an [[accepted]] table")
            continue
        name = f"entry {position} ({raw.get('id', 'no id')} in {raw.get('package', 'no package')})"
        broken = field_problems(raw)
        if broken:
            problems.extend(f"{name}: {problem}" for problem in broken)
            continue
        risk = AcceptedRisk(
            id=raw["id"],
            package=raw["package"],
            reason=raw["reason"],
            owner=raw["owner"],
            added=raw["added"],
            until=raw["until"],
            link=raw["link"],
            no_fix=raw.get("no_fix", False),
        )
        if (risk.id, risk.package) in seen:
            problems.append(f"{name}: the same advisory is accepted twice")
            continue
        seen.add((risk.id, risk.package))
        problems.extend(f"{name}: {problem}" for problem in content_problems(risk, today))
        risks.append(risk)
    return risks, problems


def load(path: Path, today: date) -> tuple[list[AcceptedRisk], list[str]]:
    if not path.exists():
        return [], [f"{path} is missing: the list of accepted risks is part of the policy"]
    return parse(tomllib.loads(path.read_text(encoding="utf-8")), today)


def main() -> int:
    if len(sys.argv) > 2:
        print("usage: accepted_risks.py [FILE]")
        return 2
    path = Path(sys.argv[1]) if len(sys.argv) == 2 else DEFAULT_PATH
    risks, problems = load(path, date.today())
    if problems:
        print(f"--- accepted risks: {path} is not a valid decision")
        for problem in problems:
            print(f"    {problem}")
        print("    an expired acceptance is not an ignore, it is a hole nobody renewed")
        return 1
    print(f"--- accepted risks: {len(risks)} entries, all valid, none expired")
    return 0


if __name__ == "__main__":
    sys.exit(main())
