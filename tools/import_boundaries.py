"""Fitness function: a service must not import the code of another service.

    python tools/import_boundaries.py [SERVICES_DIR]   ->  exit 1 on a violation

ADR-0002: services share no code and talk only over the network. The rule is
cheap to break by accident - "just import the model from the neighbour" is one
line and saves ten minutes, and from that moment the two services can only be
released together: the contract between them is gone, and with it every
contract test that was supposed to protect it. Review catches this while
somebody still remembers the rule; a gate catches it always.

What counts as a service and what it owns is read from the tree itself: a
service is a directory in services/, its own packages are the directories in
its src/. So a new service is covered the day it appears, without editing a
list here.

Imports are taken from the syntax tree, not from a regular expression: a name
in a string or a comment is not an import, `import a.b` and `from a.b import c`
are the same dependency, and a relative import cannot leave the service at all.
"""

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Copies of the code (mutmut), caches and installed dependencies: not the
# sources of the service, and mutants would report the same line many times.
SKIPPED_DIRS = frozenset({".venv", "mutants", "__pycache__", "node_modules"})


@dataclass(frozen=True)
class Violation:
    """One import that crosses the boundary between two services."""

    path: Path
    line: int
    imported: str
    owner: str


def owned_packages(service_dir: Path) -> set[str]:
    """Packages the service publishes in its own src/: everything it may import."""
    return {path.name for path in (service_dir / "src").glob("*") if path.is_dir()}


def package_owners(services_root: Path) -> dict[str, str]:
    """Package name -> the service that owns it."""
    return {
        package: service_dir.name
        for service_dir in service_dirs(services_root)
        for package in owned_packages(service_dir)
    }


def service_dirs(services_root: Path) -> list[Path]:
    return sorted(path for path in services_root.iterdir() if path.is_dir())


def python_files(services_root: Path) -> Iterator[tuple[str, Path]]:
    """Every Python file of every service, tests included: a forbidden import in
    a test binds the services together just as hard as one in the source."""
    for service_dir in service_dirs(services_root):
        for path in sorted(service_dir.rglob("*.py")):
            relative = path.relative_to(service_dir)
            if SKIPPED_DIRS.isdisjoint(relative.parts):
                yield service_dir.name, path


def imported_roots(tree: ast.Module) -> list[tuple[int, str]]:
    """Line and root module of every import: `import a.b` and `from a.b import c`
    are both a dependency on "a"."""
    roots: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend((node.lineno, alias.name.split(".")[0]) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            # level > 0 is a relative import: it stays inside the package.
            roots.append((node.lineno, node.module.split(".")[0]))
    return roots


def find_violations(services_root: Path) -> list[Violation]:
    owners = package_owners(services_root)
    violations: list[Violation] = []
    for service, path in python_files(services_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, root in imported_roots(tree):
            owner = owners.get(root)
            if owner is not None and owner != service:
                violations.append(Violation(path, line, root, owner))
    return violations


def describe(violation: Violation, services_root: Path) -> str:
    path = violation.path.relative_to(services_root.parent)
    return f"{path}:{violation.line}: imports '{violation.imported}' of service {violation.owner}"


def main() -> int:
    if len(sys.argv) > 2:
        print("usage: import_boundaries.py [SERVICES_DIR]")
        return 2
    services_root = Path(sys.argv[1]) if len(sys.argv) == 2 else REPO_ROOT / "services"
    violations = find_violations(services_root)
    if violations:
        print("--- import boundaries: services import each other (ADR-0002)")
        for violation in violations:
            print(f"    {describe(violation, services_root)}")
        print(
            "    services share no code: describe the contract and call it over"
            " HTTP or publish an event"
        )
        return 1
    checked = sum(1 for _ in python_files(services_root))
    print(f"--- import boundaries: {checked} files, no service imports another")
    return 0


if __name__ == "__main__":
    sys.exit(main())
