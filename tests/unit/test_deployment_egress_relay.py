"""L1 — the IF-10 egress relay guard can actually fail (SRS-NOTIF-001).

`phase1-notification-egress` is configured by a shell script, so nothing in the
type system holds it to the contract `crates/atp-adapters/src/notification/smtp.rs`
enforces at runtime. `tools/deployment_check.py` is that guard, and this file is
the proof it discriminates: every case below is a relay that STARTS CLEANLY and
passes every scripted-relay test in `srs_notif_001_transports.rs`, because the
scripted relay is a double written to match the adapter rather than Postfix.

A check with no proof it can fail is the same false green as any other
(docs/playbooks/security-boundaries.md rule 22).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run_check(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "tools/deployment_check.py", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_the_real_tree_satisfies_the_egress_relay_contract() -> None:
    result = run_check()
    assert result.returncode == 0, result.stdout + result.stderr
    # Assert the specific evidence, not just the exit code: a check that stopped
    # examining the relay would still exit 0.
    assert "phase1-notification-egress advertises AUTH on the plaintext hop" in result.stdout
    assert "never permits mynetworks" in result.stdout
    assert "publishes no host port" in result.stdout
    assert "pins its base image by digest" in result.stdout


@pytest.mark.parametrize(
    ("fixture", "expected_detail"),
    [
        # The adapter refuses a relay whose EHLO omits AUTH (smtp.rs:200).
        ("egress-no-auth", "smtpd_sasl_auth_enable = yes"),
        # The subtle one: Postfix's DEFAULT withholds AUTH until STARTTLS, which
        # the adapter never issues. Every setting looks right; no alert can send.
        ("egress-tls-auth-only", "smtpd_tls_auth_only = no"),
        # An open relay lets any peer forge an operator alert without the key.
        ("egress-permit-mynetworks", "permits mynetworks"),
        # A host-published submission port is still an RFC 1918 address, so
        # EgressEndpoint's private-address rule does not constrain who reaches it.
        ("egress-published-port", "publishes a host port"),
        # Merging *atp-env hands a third-party MTA every catalogued secret.
        ("egress-atp-env", "merges *atp-env"),
    ],
)
def test_the_guard_rejects_each_bypass_class(fixture: str, expected_detail: str) -> None:
    result = run_check("--fixture", fixture)
    assert result.returncode == 1, (
        f"fixture {fixture!r} did not fail the check. A guard that cannot fail is "
        f"not evidence.\n{result.stdout}\n{result.stderr}"
    )
    assert "SRS-ARCH-004 FAIL" in result.stderr
    assert expected_detail in result.stderr, result.stderr


def test_an_inert_fixture_raises_rather_than_reporting_a_clean_tree() -> None:
    """A fixture whose anchor stops matching must fail LOUDLY.

    This is the failure mode that looks most like the guard working: the fixture
    silently rewrites nothing, the check examines an intact tree and passes, and
    the test asserting "the fixture fails" is the only thing that goes red — with
    a message about the CHECK rather than about the fixture.
    `make_fixture_root` raises instead, and this pins that.
    """

    from tools.deployment_check import _EGRESS_ENTRYPOINT_FIXTURES, make_fixture_root

    fixture = "egress-no-auth"
    anchor, _ = _EGRESS_ENTRYPOINT_FIXTURES[fixture]
    original = _EGRESS_ENTRYPOINT_FIXTURES[fixture]
    _EGRESS_ENTRYPOINT_FIXTURES[fixture] = (anchor + "-no-longer-present", "irrelevant")
    try:
        with pytest.raises(ValueError, match="inert"):
            make_fixture_root(fixture)
    finally:
        _EGRESS_ENTRYPOINT_FIXTURES[fixture] = original
