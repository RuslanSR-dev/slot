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
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "security" / "gitleaks.toml"

# Every literal the scanner is allowed to stay silent about. The reason for
# each one is in security/gitleaks.toml, next to the exclusion itself.
ALLOWED_SECRETS = frozenset(
    {
        "^test-api-key$",
        "^test-webhook-secret$",
        "^webhook-secret$",
        "^local-webhook-secret$",
    }
)

# Paths that hold no source of ours: build output and installed dependencies.
ALLOWED_PATHS = frozenset(
    {
        r"(^|/)\.venv/",
        r"(^|/)\.tmp/",
        r"(^|/)\.git/",
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
        if not (regex.startswith("^") and regex.endswith("$")) or re.search(r"[.*+?]", regex[1:-1])
    ]

    assert loose == []
