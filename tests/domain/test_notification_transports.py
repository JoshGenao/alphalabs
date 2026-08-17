"""SRS-NOTIF-001 — the concrete IF-10 / IF-11 operator-alert transports must
bound their own send deadline, keep the cleartext egress hop on a private
network, refuse an unauthenticated relay, and never leak the relay credential.

L7 domain (safety) test. The transports at
``crates/atp-adapters/src/notification/`` are the email + push half of the
connectivity/critical-failure safety path (SyRS SYS-46, NFR-P6; StRS SN-1.12,
SN-2.04, SC-9): they are what actually carries an alert off the host when the IB
Gateway drops. The Rust boundary tests at
``crates/atp-adapters/tests/srs_notif_001_transports.rs`` drive them over real
loopback sockets against scripted relays; this test shells out to ``cargo test``
so the safety post-conditions are anchored in the domain layer.

``notification`` is not itself a ``SAFETY_PATH_RE`` token, so the deterministic
critic does not demand this pairing — but the AGENTS.md hard rule is about the
connectivity/critical-failure path, which this is, so the pairing is written
anyway (the same call made for ``test_notification_dispatch.py``).

The end-to-end proof over REAL providers — a genuine IB connectivity loss
dispatching a real email and a real push — is the operator integration that keeps
SRS-NOTIF-001 ``passes:false``. These tests prove the transport properties
deterministically, without a provider account.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]
RUST_TEST_TARGET = "srs_notif_001_transports"


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust boundary test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-adapters",
            "--test",
            RUST_TEST_TARGET,
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_one_passed(result: subprocess.CompletedProcess[str], label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_send_deadline_bounds_the_whole_conversation() -> None:
    # NFR-P6 arithmetic: the dispatcher clamps each channel to
    # DISPATCH_SLA_MS / REQUIRED_CHANNELS.len(), which only bounds the 60,000ms
    # SLA if the adapter spends its deadline ACROSS the whole conversation. An
    # adapter that armed the full deadline per socket operation would grant a
    # slow relay that deadline once per round trip -- seven times over, for SMTP
    # -- while every individual timeout still looked correctly armed.
    _assert_one_passed(
        _run_cargo_test("the_send_deadline_bounds_the_whole_conversation_not_each_leg"),
        "SRS-NOTIF-001 total-budget deadline",
    )


def test_a_stalled_ntfy_cannot_outlive_its_deadline() -> None:
    # Same property on the IF-11 path: a gateway that accepts the request and
    # then stalls is cut off at the deadline, not held until the socket gives up.
    _assert_one_passed(
        _run_cargo_test("a_stalled_ntfy_is_bounded_by_the_send_deadline"),
        "SRS-NOTIF-001 push deadline",
    )


def test_the_cleartext_egress_hop_cannot_leave_a_private_network() -> None:
    # The transports are plaintext by construction (the relay owns TLS), so the
    # alert body and the relay credential must never cross a public network. The
    # guard resolves and validates per connect and refuses before any packet.
    _assert_one_passed(
        _run_cargo_test("a_public_relay_address_is_refused_before_any_packet_is_sent"),
        "SRS-NOTIF-001 private-egress confinement",
    )


def test_an_unauthenticated_relay_is_refused() -> None:
    # A relay that does not authenticate its clients is an open relay: any
    # container able to route to it could send operator alerts, and the alert
    # channel would no longer be trustworthy evidence. Refuse to submit.
    _assert_one_passed(
        _run_cargo_test("a_relay_that_does_not_advertise_auth_is_refused_as_an_open_relay"),
        "SRS-NOTIF-001 open-relay refusal",
    )


def test_the_relay_credential_is_never_sent_in_the_clear() -> None:
    # NFR-S4: the credential is base64 AUTH PLAIN, and the raw key appears
    # nowhere in the conversation the relay observes.
    _assert_one_passed(
        _run_cargo_test("the_relay_credential_is_presented_as_base64_auth_plain"),
        "SRS-NOTIF-001 credential encoding",
    )


def test_an_accepted_send_never_fabricates_an_accept_id() -> None:
    # No-fabrication, matching the core dispatcher's discipline: a 2xx with no
    # body yields an explicit non-reference, never a plausible-looking id an
    # operator would hunt for in the provider's logs and never find.
    _assert_one_passed(
        _run_cargo_test("an_accepted_publish_with_no_usable_id_never_fabricates_a_reference"),
        "SRS-NOTIF-001 no fabricated accept id",
    )


def test_transient_and_permanent_failures_are_distinct_remediations() -> None:
    # A 4xx SMTP reply is retryable and a 5xx is not; collapsing them would tell
    # the operator to wait out a failure that will never clear.
    _assert_one_passed(
        _run_cargo_test("a_transient_4xx_is_reported_as_a_retryable_transport_failure"),
        "SRS-NOTIF-001 transient classification",
    )
    _assert_one_passed(
        _run_cargo_test("a_permanent_5xx_is_reported_as_a_rejection_a_retry_will_not_fix"),
        "SRS-NOTIF-001 permanent classification",
    )
