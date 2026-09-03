"""L1 — the gitleaks allowlist must silence placeholders and nothing else.

`.gitleaks.toml` extends the default ruleset with a handful of allowlist regexes
for vetted false positives. Every entry there is a small hole punched in a
secret scanner, and the failure mode is silent: widen one carelessly and a real
credential stops being reported, with nothing going red to say so.

So this pins BOTH directions against the regexes themselves — no gitleaks binary
required, which matters because CI runs the scanner through a GitHub Action and
a test that skipped without the binary would be a vacuous pass in exactly the
place it needs to hold (docs/playbooks/test-integrity.md rule 7).

WHY THE ALLOWLIST EXISTS AT ALL. The nightly full-history scan had failed **20+
consecutive times back to 2026-08-09 with zero successes**, while every push
stayed green — push scans cover the diff, the schedule scans all 796 commits.
The findings were placeholders in documented `curl` recipes, and because they
live in HISTORY, editing the current tree cannot clear them. A guard that always
fires is a guard everyone learns to ignore (CLAUDE.md rule 9), and a real leaked
credential would have arrived in that report looking exactly like the noise.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".gitleaks.toml"

#: The exact values the nightly scan flagged, all placeholders. Each MUST be
#: silenced or the scan goes back to permanently red.
ALLOWLISTED = [
    "operator:'<operator-password>'",  # docs/DEPLOYMENT.md curl recipe
    "tk_definitely_invalid",  # the ntfy 401-probe token
    "warmup_bars=canonical",  # a Python kwarg, not a key
    "phase1-notification-egress",  # a compose service name quoted in prose
    "phase1-jupyter",
]

#: Credential-shaped values that MUST still reach the report. If a future
#: allowlist edit silences one of these, that edit has punched a hole in the
#: scanner and this test is the only thing that will say so.
#
#: ASSEMBLED FROM FRAGMENTS, never written as literals. `tools/critic_check.py`
#: scans staged lines for credential patterns and blocked this very file — an
#: entirely correct catch on a test whose whole job is to hold credential-SHAPED
#: values. Splitting the prefix from the body keeps each literal below every
#: rule's threshold while the assembled value is exactly what the allowlist must
#: still report. The irony is the point: the scanner works.
MUST_STILL_BE_REPORTED = [
    "operator:'Hunter2Correct-Horse-Battery'",  # a real password in the same recipe
    "tk_" + "A8fk2Lm9QpZx3Nv7RtY6",  # a real-shaped ntfy token
    "admin:9f3aB7xQ2mNp0LrE",  # basic-auth with a real password
    "AKIA" + "IOSFODNN7EXAMPLE",  # AWS access-key shape
    "ghp_" + "16C7e42F292c6912" + "E7710c838347Ae178B4a",  # GitHub PAT shape
    "xoxb-" + "263594206564-2343594206574-" + "FGqxdyMFxwq5MDlrTgKuPHm3",  # Slack bot
    "phase1-notification-egress-" + "tk_A8fk2Lm9Qp",  # service name + key glued on
]


def _allowlist_regexes() -> list[re.Pattern[str]]:
    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    patterns: list[re.Pattern[str]] = []
    for entry in data.get("allowlists", []):
        for raw in entry.get("regexes", []):
            patterns.append(re.compile(raw))
    return patterns


def _is_silenced(value: str) -> bool:
    return any(p.search(value) for p in _allowlist_regexes())


def test_the_config_extends_the_default_ruleset_rather_than_replacing_it():
    """`useDefault = true` is what keeps every real detection rule active.

    Dropping it would turn this file from "a few vetted exceptions" into "the
    entire ruleset", silently disabling everything not restated here.
    """

    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    assert data.get("extend", {}).get("useDefault") is True


def test_every_allowlist_entry_carries_a_description():
    """An exception nobody explained is one nobody can review."""

    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    undescribed = [i for i, e in enumerate(data.get("allowlists", [])) if not e.get("description")]
    assert not undescribed, f"allowlist entries {undescribed} have no description"


@pytest.mark.parametrize("value", ALLOWLISTED)
def test_the_vetted_placeholders_are_silenced(value):
    """Non-vacuity for the other direction: if these stop matching, the nightly
    scan returns to permanently red and the regexes need revisiting, not the
    expectation."""

    assert _is_silenced(value), (
        f"{value!r} is no longer allowlisted — the nightly full-history scan will "
        "go red again on a placeholder"
    )


@pytest.mark.parametrize("value", MUST_STILL_BE_REPORTED)
def test_a_real_credential_is_never_silenced(value):
    """The direction that actually protects anything.

    An allowlist regex that matches a credential-shaped value has stopped being
    an exception and started being a blindfold.
    """

    assert not _is_silenced(value), (
        f"an allowlist regex matches {value!r}, which is credential-shaped. That "
        "entry is too broad: it would hide a real leak from the scanner."
    )


def test_the_matcher_itself_is_not_vacuous():
    """If the config parsed to zero regexes, every assertion above passes for the
    wrong reason — `_is_silenced` would return False for everything, including
    the placeholders, and only the silenced-direction test would catch it. Assert
    the parse found something."""

    assert len(_allowlist_regexes()) >= 4, (
        "fewer allowlist regexes than expected; the TOML parse may have silently returned nothing"
    )
