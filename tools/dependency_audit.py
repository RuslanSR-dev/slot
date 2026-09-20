"""Gate: known vulnerabilities in the dependencies of every project (ADR-0018).

    python tools/dependency_audit.py PROJECT [PROJECT ...]   ->  exit 1 on a blocking finding

Why `uv audit` and not pip-audit or a paid scanner:

- the data is the same free OSV database pip-audit reads, so nothing is lost;
- it reads `uv.lock` directly, so what is audited is exactly what gets
  installed, not a fresh resolution of `pyproject.toml` that may differ;
- it needs no dependency of its own. A scanner that lives in a lock file is
  one more thing to keep updated, and an out-of-date scanner reports "clean";
- cost: the command is in preview, so its output shape may change. This gate
  validates the fields it uses and fails loudly instead of quietly reporting
  "no vulnerabilities" - a scanner that cannot fail is not a gate.

Why the policy is not "block everything" (ADR-0018 has the argument in full):
a gate that goes red for a low-severity advisory in a test-only dependency,
published an hour ago, in a pull request that touches nothing related, teaches
the team one thing - how to get around the gate. After that the high-severity
finding is skipped by the same reflex. So the severity decides, the clock is
counted from the day the advisory became public, and the only way past the
gate is a written, owned, expiring entry in security/accepted-risks.toml.
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx2

from accepted_risks import DEFAULT_PATH, AcceptedRisk, load

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = REPO_ROOT / "reports" / "dependency-audit.json"

# GitHub advisory levels, which is what the OSV records carry for Python.
# UNKNOWN is what we call an advisory nobody has rated yet.
BLOCKING_SEVERITIES = frozenset({"CRITICAL", "HIGH", "UNKNOWN"})
# A moderate or low finding is shown at once and blocks when it gets this old.
# Counted from publication, not from today, so a PR is never broken by an
# advisory that appeared while it was open.
GRACE_DAYS = 30

OSV_URL = "https://api.osv.dev/v1/vulns/"
OSV_TIMEOUT_SECONDS = 20


class SeverityLookup(Protocol):
    """Severity of one advisory. Injected so the tests need no network."""

    def __call__(self, advisory: str) -> str: ...


@dataclass(frozen=True)
class Finding:
    """One vulnerable package in one project."""

    project: str
    package: str
    version: str
    advisory: str
    aliases: tuple[str, ...]
    summary: str
    fix_versions: tuple[str, ...]
    published: date
    link: str

    @property
    def identifiers(self) -> frozenset[str]:
        return frozenset({self.advisory, *self.aliases})

    @property
    def has_fix(self) -> bool:
        return bool(self.fix_versions)


@dataclass(frozen=True)
class Judgement:
    """What the policy says about one finding."""

    finding: Finding
    severity: str
    status: str  # blocks | reported | accepted
    explanation: str


def published_date(value: str) -> date:
    """OSV timestamps carry a time and sometimes nanoseconds; only the day matters."""
    return date.fromisoformat(value[:10])


def canonical_key(advisory: str, aliases: tuple[str, ...]) -> str:
    """OSV publishes the same vulnerability as a GHSA, a PYSEC and a CVE record,
    and the audit reports all three. They are one finding, and a CVE number is
    the name they share."""
    cves = sorted(name for name in (advisory, *aliases) if name.startswith("CVE-"))
    return cves[0] if cves else advisory


def preferred(left: Finding, right: Finding) -> Finding:
    """Of two records of the same vulnerability keep the GitHub one: it is the
    only source that carries a severity rating for Python packages."""
    return right if right.advisory.startswith("GHSA-") else left


def findings_of(project: str, report: dict[str, Any]) -> list[Finding]:
    """Turn one `uv audit` report into findings, one per vulnerability."""
    if "vulnerabilities" not in report or "summary" not in report:
        raise ValueError(
            f"{project}: the output of 'uv audit' has no 'vulnerabilities' - the"
            " command is in preview and its format has probably changed"
        )
    best: dict[tuple[str, str, str], Finding] = {}
    for raw in report["vulnerabilities"]:
        dependency = raw["dependency"]
        aliases = tuple(raw.get("aliases") or ())
        finding = Finding(
            project=project,
            package=dependency["name"],
            version=dependency["version"],
            advisory=raw["id"],
            aliases=aliases,
            summary=raw.get("summary") or raw.get("description") or "",
            fix_versions=tuple(raw.get("fix_versions") or ()),
            published=published_date(raw["published"]),
            link=raw.get("link") or f"{OSV_URL}{raw['id']}",
        )
        key = (finding.package, finding.version, canonical_key(finding.advisory, aliases))
        known = best.get(key)
        best[key] = finding if known is None else preferred(known, finding)
    return sorted(best.values(), key=lambda finding: (finding.package, finding.advisory))


def audit_project(project: Path) -> dict[str, Any]:
    """Run `uv audit` over the lock file of one project."""
    result = subprocess.run(
        [
            "uv",
            "audit",
            "--frozen",
            "--output-format",
            "json",
            "--preview-features",
            "audit-command,json-output",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    # Exit code 1 only means "vulnerabilities found"; anything else is a broken run.
    if result.returncode not in (0, 1):
        raise ValueError(f"{project}: 'uv audit' failed ({result.returncode}): {result.stderr}")
    try:
        document: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"{project}: 'uv audit' printed no JSON: {result.stdout[:200]}") from error
    return document


def osv_severity(advisory: str) -> str:
    """Severity of an advisory from the free OSV API.

    The audit itself reports no severity, and the vector strings in OSV need
    the CVSS formula to become a number. The GitHub records carry a rating that
    is already a word, and after deduplication the advisory is a GHSA one.
    Anything we cannot read is UNKNOWN, and UNKNOWN blocks: a gate that guesses
    "probably low" is worse than no gate.
    """
    response = httpx2.get(f"{OSV_URL}{advisory}", timeout=OSV_TIMEOUT_SECONDS)
    if response.status_code != httpx2.codes.OK:
        return "UNKNOWN"
    specific = response.json().get("database_specific") or {}
    severity = specific.get("severity")
    return severity.upper() if isinstance(severity, str) else "UNKNOWN"


def judge(finding: Finding, severity: str, risks: list[AcceptedRisk], today: date) -> Judgement:
    accepted = [risk for risk in risks if risk.covers(finding.package, finding.identifiers)]
    if accepted:
        risk = accepted[0]
        return Judgement(
            finding,
            severity,
            "accepted",
            f"accepted by {risk.owner} until {risk.until}: {risk.reason}",
        )
    if severity in BLOCKING_SEVERITIES:
        return Judgement(finding, severity, "blocks", "severity blocks the change at once")
    age = (today - finding.published).days
    if age > GRACE_DAYS:
        return Judgement(
            finding,
            severity,
            "blocks",
            f"published {age} days ago, the deadline was {GRACE_DAYS} days",
        )
    return Judgement(finding, severity, "reported", f"{GRACE_DAYS - age} days left to fix it")


def unused_acceptances(risks: list[AcceptedRisk], findings: list[Finding]) -> list[AcceptedRisk]:
    """An acceptance nobody reports any more hides nothing and explains nothing;
    it only makes the next reader believe the repository still has that hole."""
    return [
        risk
        for risk in risks
        if not any(risk.covers(finding.package, finding.identifiers) for finding in findings)
    ]


def describe(judgement: Judgement) -> str:
    finding = judgement.finding
    fix = ", ".join(finding.fix_versions) if finding.has_fix else "none yet"
    return (
        f"{finding.project}: {finding.package} {finding.version} - {judgement.severity}"
        f" {finding.advisory} ({judgement.explanation})\n"
        f"        fix: {fix} | {finding.summary[:120]} | {finding.link}"
    )


def report_document(judgements: list[Judgement], stale: list[AcceptedRisk]) -> dict[str, Any]:
    return {
        "findings": [
            {
                "project": judgement.finding.project,
                "package": judgement.finding.package,
                "version": judgement.finding.version,
                "advisory": judgement.finding.advisory,
                "aliases": list(judgement.finding.aliases),
                "severity": judgement.severity,
                "status": judgement.status,
                "explanation": judgement.explanation,
                "fix_versions": list(judgement.finding.fix_versions),
                "published": judgement.finding.published.isoformat(),
                "link": judgement.finding.link,
            }
            for judgement in judgements
        ],
        "stale_acceptances": [risk.id for risk in stale],
    }


def collect(projects: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for project in projects:
        findings.extend(findings_of(project.name, audit_project(project)))
    return findings


def run(projects: list[Path], lookup: SeverityLookup, today: date) -> int:
    risks, problems = load(DEFAULT_PATH, today)
    if problems:
        print("--- dependency audit: the list of accepted risks is not usable")
        for problem in problems:
            print(f"    {problem}")
        return 1

    findings = collect(projects)
    judgements = [judge(finding, lookup(finding.advisory), risks, today) for finding in findings]
    stale = unused_acceptances(risks, findings)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report_document(judgements, stale), indent=2) + "\n")

    for status, title in (
        ("blocks", "must be fixed before this change is merged"),
        ("reported", "known, inside the deadline"),
        ("accepted", "accepted risk, see security/accepted-risks.toml"),
    ):
        selected = [judgement for judgement in judgements if judgement.status == status]
        if selected:
            print(f"--- dependency audit: {title}")
            for judgement in selected:
                print(f"    {describe(judgement)}")
    for risk in stale:
        print(
            f"--- dependency audit: {risk.id} in {risk.package} is accepted but no"
            " longer reported - delete the entry instead of leaving it as decoration"
        )

    blocking = sum(1 for judgement in judgements if judgement.status == "blocks")
    if blocking or stale:
        print(f"    report: {REPORT_PATH.relative_to(REPO_ROOT)}")
        return 1
    projects_names = ", ".join(project.name for project in projects)
    print(f"--- dependency audit: {projects_names}: nothing blocks, {len(findings)} findings known")
    return 0


def main() -> int:
    projects = [Path(argument) for argument in sys.argv[1:]]
    if not projects:
        print("usage: dependency_audit.py PROJECT [PROJECT ...]")
        return 2
    try:
        return run(projects, osv_severity, date.today())
    except ValueError as error:
        print(f"--- dependency audit: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
