"""The list of exclusions of the secret scanner must not grow quietly.

A scanner is only as strong as the list of things it has been told to ignore.
That list grows the same way everywhere: a build is red, the value "is only a
test one", somebody adds a line, and nobody ever reads it again. Two years
later the scanner is decoration and the repository has a real key in it.

So the exclusions are pinned here. Adding one means editing this file too, and
that shows up in review as a line somebody has to justify - which is exactly
the conversation the gate exists to start.
"""

import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "security" / "gitleaks.toml"
# Whole tokens that were committed to contracts/auth/token.v1.json before
# the file started keeping the claims and the signature apart. They live
# only in the history now, and the history is what the scanner reads.
TOKEN_VECTORS = [
    "v1.eyJleHAiOjE3OTAwMDAwMDAsInJvbGUiOiJtYXN0ZXIiLCJzdWIiOiJhbm5hIn0=.oWkUYn-rHJN545C2CCbqVu2RMAeLYXunD1a75MWZz04=",
    "v1.eyJleHAiOjE3OTAwMDAwMDAsInJvbGUiOiJjbGllbnQiLCJzdWIiOiJjbGllbnQtNDIifQ==._FGagf0O-86dy0P8XYlk1OchGAec5klxZmYWwRaqeqw=",
    "v1.eyJleHAiOjIwMDAwMDAwMDAsInJvbGUiOiJjbGllbnQiLCJzdWIiOiJvLmJyaWVuOjFAc2xvdCJ9.La9TQWjEabsV8xgJWQZMKTtaNetDAY3IQ1gB8GsnPHY=",
    "v1.eyJleHAiOjE3OTAwMDAwMDAsInJvbGUiOiJtYXN0ZXIiLCJzdWIiOiJhbm5hIn0.7wHvUKSzyxxlORZ7nHcZ_adNOdQlLBgtA5xKuIQyrsQ",
    "v1.eyJleHAiOjE3OTAwMDAwMDAsInJvbGUiOiJjbGllbnQiLCJzdWIiOiJjbGllbnQtNDIifQ.abEufZQHhyCE92OFf6Mw6BKlIaQ4LW8Q8NRrsPzQhNU",
    "v1.eyJleHAiOjIwMDAwMDAwMDAsInJvbGUiOiJjbGllbnQiLCJzdWIiOiJvLmJyaWVuOjFAc2xvdCJ9.La9TQWjEabsV8xgJWQZMKTtaNetDAY3IQ1gB8GsnPHY",
]

# Every literal the scanner is allowed to stay silent about. The reason for
# each one is in security/gitleaks.toml, next to the exclusion itself.
ALLOWED_SECRETS = frozenset(
    {
        "^test-api-key$",
        "^test-webhook-secret$",
        "^webhook-secret$",
        "^local-webhook-secret$",
        "^local-auth-secret$",
        "^golden-secret-for-the-vectors$",
        "^component-test-secret$",
        "^unit-test-secret$",
        "^test-secret$",
        "^attacker-secret$",
    }
    # The recorded token vectors of contracts/auth/token.v1.json, signed with
    # the golden test secret. Listed by value like everything else, so a real
    # token can never hide behind "it looks like one of ours".
    | {"^" + token.replace(".", r"\.") + "$" for token in TOKEN_VECTORS}
)

# Paths that hold no source of ours: build output, caches, installed
# dependencies and the working copies of parallel agents.
ALLOWED_PATHS = frozenset(
    {
        r"(^|/)\.venv/",
        r"(^|/)\.tmp/",
        r"(^|/)\.git/",
        r"(^|/)\.claude/",
        r"(^|/)__pycache__/",
        r"(^|/)\.pytest_cache/",
        r"(^|/)\.mypy_cache/",
        r"(^|/)\.ruff_cache/",
        r"(^|/)\.hypothesis/",
        r"(^|/)mutants/",
        r"(^|/)reports/",
        r"(^|/)node_modules/",
    }
)

# Rules written for this repository. The default rules of gitleaks cover other
# people's tokens; these two cover the secrets we are able to leak ourselves.
OWN_RULES = frozenset({"slot-secret-literal", "slot-secret-default"})

# Short enough to write, long enough that "test value" alone does not pass.
MIN_DESCRIPTION_CHARS = 60


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    document: dict[str, Any] = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return document


def allowlists(config: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = config["allowlists"]
    return entries


def test_default_rules_stay_switched_on(config: dict[str, Any]) -> None:
    """Our two rules are an addition, not a replacement."""
    assert config["extend"]["useDefault"] is True


def test_own_rules_are_all_there(config: dict[str, Any]) -> None:
    assert {rule["id"] for rule in config["rules"]} == OWN_RULES


def test_no_secret_is_silenced_beyond_the_pinned_list(config: dict[str, Any]) -> None:
    silenced = {
        regex
        for entry in allowlists(config)
        if entry.get("regexTarget") == "secret"
        for regex in entry.get("regexes", [])
    }

    assert silenced == ALLOWED_SECRETS


def test_no_path_is_silenced_beyond_the_pinned_list(config: dict[str, Any]) -> None:
    """A path exclusion silences every secret that file will ever hold, so the
    list of them is shorter than the list of values and stays that way."""
    silenced = {path for entry in allowlists(config) for path in entry.get("paths", [])}

    assert silenced == ALLOWED_PATHS


def test_every_exclusion_says_why_it_is_safe(config: dict[str, Any]) -> None:
    too_short = [
        entry.get("description", "")
        for entry in allowlists(config)
        if len(entry.get("description", "")) < MIN_DESCRIPTION_CHARS
    ]

    assert too_short == []


def test_no_exclusion_matches_everything(config: dict[str, Any]) -> None:
    """An exclusion is one known string, not a shape. `^sk-.*$` would silence
    every key of that provider, including the real one."""
    loose = [
        regex
        for entry in allowlists(config)
        if entry.get("regexTarget") == "secret"
        for regex in entry.get("regexes", [])
        # Escaped characters are literals; only unescaped ones make a shape.
        if not (regex.startswith("^") and regex.endswith("$"))
        or re.search(r"[.*+?]", re.sub(r"\\.", "", regex[1:-1]))
    ]

    assert loose == []


def test_no_committed_file_is_hidden_by_a_path_exclusion(config: dict[str, Any]) -> None:
    """The point of the path list is to skip what is not in the repository.

    If a tracked file ever matched one of these patterns, the scanner would
    stop looking at real source - which is exactly the failure a path
    exclusion is supposed to be too blunt to cause.
    """
    tracked = subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    ).stdout.split()
    patterns = [re.compile(path) for entry in allowlists(config) for path in entry.get("paths", [])]

    hidden = [path for path in tracked if any(pattern.search(path) for pattern in patterns)]

    assert hidden == []
