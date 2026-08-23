"""SRS-NOTIF-001 / SRS-SAFE-003 — a blocked live submission must page the
operator over email AND push, must not page for planned maintenance, and must not
be silenceable by a forged flag or drowned by a retry storm.

L7 domain (safety) test. ``crates/atp-orchestrator/src/connectivity_notification.rs``
binds ``atp-execution``'s ERR-2 connectivity gate to the SRS-NOTIF-001 dispatcher —
the path that tells a human the platform has lost its broker. Its Rust unit tests
drive the real sink over real dispatcher and store objects; this test shells out
to ``cargo test`` so the safety post-conditions are anchored in the domain layer.

The file path matches ``SAFETY_PATH_RE`` (``connectivity``), so the deterministic
critic requires this pairing — but it would be warranted regardless: a missed
connectivity alert means the operator does not learn the platform stopped
trading.

NOT proven here, and not claimable from this path:

* That connectivity loss is DETECTED WHEN IT HAPPENS. The trigger is a blocked
  live submission, not the disconnect — so a Gateway that drops while no order is
  being routed goes unnoticed until the next blocked order, which may be seconds
  later or never. The stamped detection instant is therefore the OBSERVATION
  instant, and the stored dispatch latency measures observation-to-dispatch, not
  loss-to-dispatch. Reading it as NFR-P6 compliance for the loss would credit
  this path with something it has not shown.
  Closing that needs a connectivity-loss producer watching the gateway
  continuously, which needs the live-IB inbound surface that does not exist yet
  (owners: SRS-MD-003 heartbeat monitor, SRS-EXE-001 execution runtime).
* That a REAL IB Gateway outage drives this path at all, or that the relay
  delivered to a real mailbox and handset. Those are the operator run that flips
  SRS-NOTIF-001.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust unit test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-orchestrator",
            "--lib",
            f"connectivity_notification::tests::{test_name}",
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_cargo_bin_test(test_name: str) -> subprocess.CompletedProcess[str]:
    """Run one unit test inside the operator-alert BINARY target."""

    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust unit test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-orchestrator",
            "--bin",
            "notif001_operator_alert_cli",
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
    assert "1 passed" in combined, f"unexpected cargo test output:\n{combined}"


def test_an_unreachable_gateway_pages_over_both_required_channels() -> None:
    # SYS-46: the whole point of the feature. A blocked live submission reaches
    # the operator on email AND push, inside the NFR-P6 60s dispatch budget.
    _assert_one_passed(
        _run_cargo_test("an_unreachable_gateway_dispatches_over_both_required_channels"),
        "SRS-NOTIF-001 connectivity page",
    )


def test_planned_maintenance_is_suppressed_but_still_recorded() -> None:
    # SYS-75: a scheduled restart is not a fault. Nothing is sent, but the stored
    # event records both channels as Suppressed -- proof the dispatcher CHOSE
    # silence, which a dropped alert could not produce.
    _assert_one_passed(
        _run_cargo_test("a_scheduled_restart_window_is_suppressed_not_sent"),
        "SRS-NOTIF-001 restart-window suppression",
    )


def test_a_forged_maintenance_flag_cannot_silence_a_real_outage() -> None:
    # The ConnectivityEvent carries both a state and a scheduled_restart bool.
    # Trusting the bool alone would let whoever builds the event silence a
    # genuine outage. Suppression requires both to agree; disagreement pages.
    _assert_one_passed(
        _run_cargo_test("a_scheduled_restart_flag_alone_cannot_silence_a_genuine_outage"),
        "SRS-NOTIF-001 forged-flag guard",
    )


def test_a_healthy_gateway_never_fabricates_an_outage() -> None:
    # No-fabrication: a Connected observation must not write a false outage into
    # the operator's audit trail.
    _assert_one_passed(
        _run_cargo_test("a_healthy_state_never_fabricates_an_outage_alert"),
        "SRS-NOTIF-001 no fabricated outage",
    )


def test_a_maintenance_window_cannot_silence_a_real_outage() -> None:
    # The false-all-clear case, and the sharpest one in this file. A scheduled
    # restart is suppressed -- it SENDS NOTHING -- so if it also arms the shared
    # cool-down, a genuine Unreachable arriving inside that window is coalesced
    # and the operator is never paged. A restart window is exactly when a real
    # failure is most likely and least distinguishable from the planned
    # disconnect, which makes it the worst possible moment to go quiet.
    # Outage and maintenance now hold independent windows.
    _assert_one_passed(
        _run_cargo_test("a_maintenance_window_cannot_silence_a_real_outage_that_follows_it"),
        "SRS-NOTIF-001 maintenance must not mask an outage",
    )


def test_an_outage_does_not_consume_the_maintenance_budget() -> None:
    # The converse direction of the same independence property.
    _assert_one_passed(
        _run_cargo_test("an_outage_does_not_consume_the_maintenance_windows_budget"),
        "SRS-NOTIF-001 window independence",
    )


def test_a_retry_storm_pages_once_and_admits_what_it_folded() -> None:
    # The sink fires once per BLOCKED ORDER, not once per outage. Without
    # coalescing, a retry loop pages hundreds of times, burns the push service's rate limit, and
    # buries the first useful alert. Coalescing is never silent -- the count
    # rides in the next alert, so a storm cannot read as one isolated block.
    _assert_one_passed(
        _run_cargo_test("a_retry_storm_pages_once_and_reports_the_coalesced_count"),
        "SRS-NOTIF-001 alert-storm control",
    )


def test_the_cooldown_is_armed_by_the_attempt_not_by_success() -> None:
    # A provider outage is exactly when every send fails. Arming the cool-down on
    # success would leave a broken provider un-rate-limited.
    _assert_one_passed(
        _run_cargo_test("the_cooldown_is_armed_by_the_attempt_not_by_success"),
        "SRS-NOTIF-001 cool-down arming",
    )


def test_recording_an_alert_does_not_delay_the_reconnect() -> None:
    # `record` runs inside ExecutionEngine::submit_live_order, which calls
    # connectivity.request_reconnect() immediately AFTER it. Dispatching inline
    # would put up to two per-channel send deadlines between detecting the outage
    # and asking to reconnect -- so reporting the problem would extend it, and the
    # strategy's order path would stall for the same span. The network work is
    # moved off the caller's thread; callers that need the outcome flush().
    #
    # There is deliberately NO inline fallback when the worker cannot be spawned:
    # spawn failure means resource exhaustion, so doing the I/O inline would put
    # ~40s in front of the reconnect at the worst possible moment. Recovery wins
    # over the page, and the miss is recorded rather than silent.
    _assert_one_passed(
        _run_cargo_test("record_returns_immediately_even_when_the_channels_are_slow"),
        "SRS-NOTIF-001 non-blocking record",
    )


def test_a_failed_alert_worker_does_not_silence_the_next_outage() -> None:
    # The cool-down must be armed only once the worker is actually running. Armed
    # before that, a transient resource spike suppresses the page for the whole
    # 5-minute window on a dispatch that never began -- the operator hears nothing
    # about a live outage and no durable event is written either.
    _assert_one_passed(
        _run_cargo_test("a_failed_worker_spawn_does_not_arm_the_cooldown"),
        "SRS-NOTIF-001 failed-spawn must not suppress",
    )


def test_the_alert_does_not_pass_observation_off_as_detection() -> None:
    # The trigger fires on a blocked submission, so the gateway may have been down
    # well before the alert was written. If the text reads as though the loss was
    # caught when it happened, the stored dispatch latency gets read as
    # loss-to-dispatch and this path is credited with an NFR-P6 compliance it has
    # not shown. The caveat is part of the alert, so it is pinned.
    _assert_one_passed(
        _run_cargo_test("the_alert_text_says_the_state_was_observed_not_detected_at_the_loss"),
        "SRS-NOTIF-001 observation-vs-detection honesty",
    )


def test_a_failing_transport_never_panics_the_execution_path() -> None:
    # This sink runs INSIDE the execution engine's order-rejection path. A panic
    # here would turn a transport hiccup into an unusable engine.
    _assert_one_passed(
        _run_cargo_test("a_failing_transport_is_recorded_and_never_panics_the_execution_path"),
        "SRS-NOTIF-001 non-panicking sink",
    )


def test_the_delivery_status_is_durably_stored() -> None:
    # The AC's second half: delivery status is stored as a notification event.
    _assert_one_passed(
        _run_cargo_test("the_stored_event_is_appended_to_the_durable_audit_store"),
        "SRS-NOTIF-001 durable storage",
    )


def test_the_stored_sla_evidence_cannot_describe_a_dispatch_that_never_started() -> None:
    """SRS-NOTIF-001 AC / NFR-P6: the stored latency must be trustworthy.

    The acceptance criterion is "notification dispatch begins within 60 seconds
    of detection and delivery status is stored as a notification event", and the
    stored ``dispatch_latency_millis`` is the evidence for the first half. The
    dispatch runs on a worker thread (so reporting an outage cannot delay
    recovery from it), which means the instant recorded as the dispatch start has
    to be read BY that worker. Stamped before the spawn instead, a worker held up
    by scheduler pressure or resource exhaustion records ~0 ms and passes the SLA
    while nothing has been sent — evidence that actively asserts a false green,
    which is worse for an operator than no evidence at all.
    """

    _assert_one_passed(
        _run_cargo_test("the_stored_dispatch_latency_reflects_when_the_worker_actually_started"),
        "SRS-NOTIF-001 non-falsifiable SLA evidence",
    )


def test_a_malformed_push_topic_is_refused_at_startup_and_never_echoed() -> None:
    """SRS-NOTIF-001 / NFR-S4: the topic's SHAPE is a readiness property.

    The transport requires ntfy's topic alphabet because the topic becomes the
    URL path — a `/`, `?`, `#`, space or control character would retarget the
    publish or split the request line. If startup only checked non-empty, a topic
    like `topic/with/slash` would pass readiness and fail at send time: the alert
    path is broken and the only thing that would say so is the page that never
    arrived.

    The failure reason must NOT contain the value. `ATP_PUSH_TOPIC` is a publish
    credential (holding it is enough to send), and readiness reasons are emitted
    to logs and the dashboard.
    """

    import sys
    from pathlib import Path

    python_root = Path(__file__).resolve().parents[2] / "python"
    if str(python_root) not in sys.path:
        sys.path.insert(0, str(python_root))
    from atp_config import REQUIRED_KEYS, load_and_validate

    def topic_errors(topic: str) -> list[str]:
        env = {s.name: s.default for s in REQUIRED_KEYS if s.default is not None}
        env["ATP_PUSH_TOPIC"] = topic
        report = load_and_validate(env)
        return [f.reason for f in report.errors if f.key == "ATP_PUSH_TOPIC"]

    for good in ("atp-alerts-9f8e7d6c5b4a3210", "a_b-C9", "A" * 64):
        assert not topic_errors(good), f"{good!r} must pass readiness"

    for bad in (
        "topic/with/slash",
        "topic with space",
        "topic?query",
        "topic#frag",
        "topic\r\nX-Evil: 1",
        "café",
        "topic:8080",
    ):
        reasons = topic_errors(bad)
        assert reasons, f"{bad!r} must FAIL readiness before any alert is due"
        assert bad not in reasons[0], f"the topic leaked into the reason: {reasons[0]}"


def test_a_public_push_host_is_refused_at_startup_not_at_alert_time() -> None:
    """SRS-NOTIF-001 / NFR-P6: the push endpoint must fail readiness, not the page.

    The transport refuses non-private egress at send time (the alert body and a
    bearer token would otherwise leave the private network). If startup accepted
    a public host, the ONLY thing that would reveal the misconfiguration is the
    connectivity-loss alert that never arrives — the failure surfaces during the
    incident it was supposed to report. So the catalogue validator has to reject
    it first, and with the SAME policy the adapter enforces.

    Link-local is refused deliberately: 169.254.169.254 is the cloud metadata
    endpoint, and the transport sends its credential immediately after connect.
    """

    import sys
    from pathlib import Path

    python_root = Path(__file__).resolve().parents[2] / "python"
    if str(python_root) not in sys.path:
        sys.path.insert(0, str(python_root))
    from atp_config import REQUIRED_KEYS, load_and_validate

    def env_with(host: str) -> dict[str, str]:
        env = {s.name: s.default for s in REQUIRED_KEYS if s.default is not None}
        env["ATP_PUSH_HOST"] = host
        return env

    def push_errors(host: str) -> list[str]:
        report = load_and_validate(env_with(host))
        return [f.reason for f in report.errors if f.key == "ATP_PUSH_HOST"]

    # Reachable only over a private network — accepted.
    for allowed in (
        "127.0.0.1",
        "10.1.2.3",
        "172.16.4.1",
        "192.168.1.10",
        "::1",
        "fc00::1",
        "::ffff:192.168.1.10",
    ):
        assert not push_errors(allowed), f"{allowed} must pass readiness"

    # A HOSTNAME is refused outright. Deferring it to the adapter was the first
    # attempt and it left the hole this check exists to close: the name would
    # pass readiness and fail at send time, i.e. during the incident. Resolving
    # it here is not an option — load_and_validate is pure by contract — and a
    # name that resolves privately at startup can resolve elsewhere by the time
    # an alert is dispatched.
    for hostname in ("ntfy.lan", "ntfy.example.com", "localhost"):
        reasons = push_errors(hostname)
        assert reasons, f"{hostname} must FAIL readiness — a name is not decidable"
        assert "IP address literal" in reasons[0], reasons

    # Public, carrier-grade NAT, link-local, and the IPv4-mapped forms — refused.
    for refused in (
        "8.8.8.8",
        "1.1.1.1",
        "169.254.169.254",
        "172.32.0.1",
        "100.64.0.1",
        "2001:4860:4860::8888",
        "fe80::1",
        "::ffff:8.8.8.8",
    ):
        reasons = push_errors(refused)
        assert reasons, f"{refused} must FAIL readiness before any alert is due"
        assert "public network" in reasons[0], reasons


def test_the_operator_alert_binary_refuses_placeholder_credentials_in_production() -> None:
    """SRS-SEC-001 / SRS-NOTIF-001: never publish an alert with a placeholder.

    The documented deployment flow (.env.example, docs/DEPLOYMENT.md) tells the
    operator to seal ATP_SMTP_API_KEY / ATP_PUSH_TOPIC / ATP_PUSH_TOKEN in the
    encrypted vault and LEAVE THE PLACEHOLDERS in `.env`. The operator alert
    binary reads the process environment directly and cannot open that vault —
    it is the Rust composition root, the vault is a Python component — so
    following the documented flow would otherwise have published an operator
    alert authenticated with the literal string `placeholder-set-in-environment`.

    The binary therefore enforces the half of the readiness contract available to
    it: the same placeholder rejection `atp_config` applies, at the same severity
    and in the same environments, naming the offending keys and never printing
    their values. Development keeps the flexibility init.sh depends on.
    """

    _assert_one_passed(
        _run_cargo_bin_test("tests::placeholder_credentials_are_refused_in_staging_and_production"),
        "SRS-NOTIF-001 no placeholder credentials in production",
    )


def test_every_surface_agrees_on_the_push_endpoint_default() -> None:
    """A documented default the code does not implement is a lie with a fuse.

    SRS-NOTIF-001 / SRS-ARCH-005. `ATP_PUSH_PORT` is optional, so an operator who
    trusts the documentation and omits it gets whatever the TRANSPORT decided —
    and if that disagrees with the catalogue, the disagreement surfaces as the
    connectivity-loss alert that never arrives.

    This is not hypothetical. Moving the port off a collision with the dashboard
    API's 8080, five documented surfaces were updated and the Rust constant was
    not; the operator CLI's own --help then repeated the stale value as a sixth.
    Adversarial review caught it. This test is what makes the next one impossible
    to land.
    """

    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]

    def catalogue_default() -> str:
        blob = json.loads((root / "architecture" / "runtime_services.json").read_text())

        def walk(node: object) -> dict | None:
            if isinstance(node, dict):
                if node.get("name") == "ATP_PUSH_PORT":
                    return node
                for value in node.values():
                    if (hit := walk(value)) is not None:
                        return hit
            elif isinstance(node, list):
                for value in node:
                    if (hit := walk(value)) is not None:
                        return hit
            return None

        spec = walk(blob)
        assert spec is not None, "ATP_PUSH_PORT is not in the ARCH-005 catalogue"
        return str(spec["default"])

    def one(pattern: str, path: str) -> str:
        text = (root / path).read_text(encoding="utf-8")
        match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
        assert match is not None, f"{pattern!r} not found in {path}"
        return match.group(1)

    surfaces = {
        "catalogue": catalogue_default(),
        ".env.example": one(r"^ATP_PUSH_PORT=(\d+)", ".env.example"),
        "compose anchor": one(r"ATP_PUSH_PORT: \$\{ATP_PUSH_PORT:-(\d+)\}", "docker-compose.yml"),
        "rust constant": one(
            r"const DEFAULT_PUSH_PORT: u16 = (\d+)",
            "crates/atp-adapters/src/notification/push.rs",
        ),
        "config README": one(r"`ATP_PUSH_PORT`.*?\| `(\d+)`", "python/atp_config/README.md"),
        "operator CLI help": one(
            r"default to a loopback ntfy on port (\d+)",
            "crates/atp-orchestrator/src/bin/notif001_operator_alert_cli.rs",
        ),
    }

    assert len(set(surfaces.values())) == 1, f"ATP_PUSH_PORT default disagrees: {surfaces}"

    # The BUNDLED server's own knobs have to agree too, or the class is only
    # half closed: moving ATP_NTFY_PORT without moving ATP_PUSH_PORT leaves every
    # surface above self-consistent while the default push target points at a
    # closed port. The two are one setting wearing two names.
    bundled = {
        "ATP_NTFY_PORT (.env.example)": one(r"^ATP_NTFY_PORT=(\d+)", ".env.example"),
        "compose published port": one(
            r"\$\{ATP_NTFY_BIND:-127\.0\.0\.1\}:\$\{ATP_NTFY_PORT:-(\d+)\}:80",
            "docker-compose.yml",
        ),
        "compose NTFY_BASE_URL": one(
            r"NTFY_BASE_URL: http://\$\{ATP_NTFY_BIND:-127\.0\.0\.1\}:\$\{ATP_NTFY_PORT:-(\d+)\}",
            "docker-compose.yml",
        ),
    }
    assert set(bundled.values()) == set(surfaces.values()), (
        f"the bundled ntfy port and the push default disagree: {bundled} vs {surfaces['catalogue']}"
    )

    # And it must not be the dashboard API's published port: ATP_PUSH_HOST
    # defaults to loopback, so colliding here aims a default-config alert — body
    # and bearer token — at the dashboard instead of at ntfy.
    dashboard = one(r'- "127\.0\.0\.1:(\d+):\d+"', "docker-compose.yml")
    assert surfaces["catalogue"] != dashboard, (
        f"the push default ({surfaces['catalogue']}) collides with the dashboard "
        f"API's published port; a default alert would POST its bearer token there"
    )


def test_a_malformed_ios_upstream_is_refused_before_ntfy_starts() -> None:
    """SRS-NOTIF-001 / SN-1.12: the iOS wake-up must not fail silently.

    `ATP_NTFY_UPSTREAM` is the only thing that gets an alert to a LOCKED iPhone,
    and ntfy gives no signal about it at all — an empty value, a valid URL and
    `not-a-url` produce byte-identical startup logs (measured against 2.27.0). A
    typo therefore does not fail; it silently downgrades push to foreground-only
    while ATP keeps publishing 200s and recording deliveries, so SYS-46 would
    report success for a page the operator never received.

    Nothing else can catch this. The key cannot live in the ARCH-005 catalogue —
    `merge_env` reads an empty value as "not provided", so a key whose normal
    state is empty always fails readiness as "not set" — so `atp_config` never
    sees it. `tools/deployment_check.py` is the preflight, and this pins it.
    """

    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]

    def check(value: str | None) -> int:
        env = dict(os.environ)
        if value is None:
            env.pop("ATP_NTFY_UPSTREAM", None)
        else:
            env["ATP_NTFY_UPSTREAM"] = value
        return subprocess.run(
            [sys.executable, "tools/deployment_check.py"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        ).returncode

    # Empty and unset both mean "no upstream", which is correct for an Android
    # target and must stay a passing configuration.
    assert check(None) == 0
    assert check("") == 0

    for good in (
        "https://ntfy.sh",
        "https://ntfy.sh/",
        "http://10.0.0.9:8080",
        "https://ntfy.example.com",
    ):
        assert check(good) == 0, f"{good} is a usable upstream and must pass"

    # Each of these is accepted silently by ntfy itself, which is the point. The
    # first four also survive a naive scheme+netloc check — `urlparse` returns
    # netloc="exa mple.com", netloc='nt"fy.sh' and netloc=":8080" without
    # complaint — so they are what make this gate worth more than its first draft.
    for bad in (
        "https://exa mple.com",
        'https://nt"fy.sh',
        "https://:8080",
        "https://ntfy.sh:99999",
        "https://ntfy.sh/some/path",
        "https://ntfy.sh?q=1",
        "https://ntfy.sh#frag",
        "not-a-url",
        "ntfy.sh",
        "ftp://ntfy.sh",
        "https://",
        "://x",
        # `parsed.hostname` strips userinfo, so these would otherwise pass with
        # host "ntfy.sh" — and put a credential in a value that is not
        # catalogued secret and does get echoed in check evidence.
        "https://user:pass@ntfy.sh",
        "https://user@ntfy.sh",
    ):
        assert check(bad) != 0, f"{bad} must be refused before ntfy is started"


def test_the_runbook_exports_the_ntfy_password_before_forwarding_it() -> None:
    """A `docker exec -e VAR` in the runbook must have an exported VAR to forward.

    The operator runs these commands verbatim, so a broken one is a broken
    deployment procedure for the SRS-NOTIF-001 alert path, not a typo.

    The specific trap: `read -rs` creates a SHELL variable, and an unexported
    shell variable is not in the docker CLI's process environment, so
    `docker exec -e NTFY_PASSWORD` has nothing to forward. Measured against ntfy
    2.27.0 — the command fails with `password: inappropriate ioctl for device`.
    The `-e VAR` form is there deliberately (it keeps the password out of argv,
    where `ps` would expose it), which is exactly why the export is easy to drop
    while the line still looks right.
    """

    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2] / "docs" / "DEPLOYMENT.md").read_text(
        encoding="utf-8"
    )
    lines = doc.splitlines()
    forwards = [i for i, line in enumerate(lines) if "docker exec -e NTFY_PASSWORD" in line]
    assert forwards, "the runbook no longer forwards NTFY_PASSWORD — has the procedure changed?"

    for i in forwards:
        window = "\n".join(lines[max(0, i - 6) : i])
        assert "export NTFY_PASSWORD" in window, (
            f"docs/DEPLOYMENT.md:{i + 1} forwards NTFY_PASSWORD to docker without an "
            "`export NTFY_PASSWORD` above it; an unexported shell variable is not in "
            "the docker CLI's environment and the command fails"
        )

    # And the password must never be an argv literal — that is what the -e form
    # is avoiding in the first place.
    assert "-e NTFY_PASSWORD=" not in doc, (
        "the runbook passes NTFY_PASSWORD as a docker argument; argv is readable "
        "by any process via `ps` (SRS-SEC-001 / NFR-S4)"
    )


def test_a_stale_empty_shell_export_cannot_silently_disable_the_ios_wakeup() -> None:
    """The trap that cost real time twice, caught mechanically.

    `set -a; . ./.env; set +a` exports the file into the shell, and the shell
    WINS over `--env-file`: compose resolves `${VAR:-default}` from the
    environment first. Editing `.env` afterwards therefore changes nothing until
    the operator re-sources — the stale value survives any number of
    `up -d --force-recreate` runs, with no error.

    Observed twice on the real VM: once binding ntfy to loopback while `.env`
    said the LAN address, once leaving the iOS wake-up disabled while `.env` said
    it was on. Both were silent, and the second one means SYS-46 reports a
    delivered page the operator never received.

    Only the HARMFUL direction is refused — an exported EMPTY value overriding a
    configured one. A deliberate override to a different URL is left alone.
    """

    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    env_file = root / ".env"
    if env_file.exists():
        pytest.skip("a real .env is present; refusing to overwrite operator config")

    def check(exported: str | None) -> int:
        env = dict(os.environ)
        if exported is None:
            env.pop("ATP_NTFY_UPSTREAM", None)
        else:
            env["ATP_NTFY_UPSTREAM"] = exported
        return subprocess.run(
            [sys.executable, "tools/deployment_check.py"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        ).returncode

    env_file.write_text("ATP_NTFY_UPSTREAM=https://ntfy.sh\n", encoding="utf-8")
    try:
        assert check("") != 0, (
            "an empty shell export silently overriding a configured .env upstream "
            "must be refused — it disables the iOS wake-up with no other signal"
        )
        assert check(None) == 0, ".env alone is a valid configuration"
        assert check("https://other.example.com") == 0, (
            "a deliberate non-empty override is not the failure mode and must not be blocked"
        )
    finally:
        env_file.unlink()
