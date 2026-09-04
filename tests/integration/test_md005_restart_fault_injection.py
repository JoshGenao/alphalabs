"""SRS-MD-005 — IB Gateway scheduled-restart fault injection (L5 integration).

Gated by ``ATP_RUN_INTEGRATION=1`` (see ``tests/conftest.py``).

This is the suite the feature's declared ``verification_method: integration``
names. The operator's own recorded rationale says it plainly: *"the criterion is
the behaviour during an outage — proven by fault injection against a dead port,
which needs no gateway."*

So the fault here is real, not stubbed. Each case binds an ephemeral loopback
port, reads its number back, releases it, and points the production
``TcpGatewayReachability`` probe at the now-dead address. Every layer between
that socket and the verdict is production code: the window arithmetic, the
``ScheduledRestartConnectivity`` producer, ``ExecutionEngine::dispatch_order``,
the subscription manager, and the SRS-NOTIF-001 dispatcher.

**It never binds 4001 or 4002.** The single-live-strategy invariant makes the IB
ports a shared resource across the agent pool, and nothing here needs them — a
restart is proven by a port that does not answer, and any ephemeral port that
does not answer will do.

## The walk

The four SYS-75 clauses are exercised in the order an operator would see them on
a real night, against ONE configured window:

1. before the lead — a healthy gateway routes and subscribes;
2. inside the lead — orders and market-data requests suspended, alert suppressed;
3. inside the window, gateway still dead — still suppressed;
4. inside the window, gateway back — normal operations resume;
5. after the window, gateway still dead — escalated to a genuine outage, paged.

Step 5 is the one the requirement turns on: a window that never closed would
suppress a real failure indefinitely, and every other step would still pass.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration

CLI_BIN = "md005_connectivity_restart_window_cli"

#: 2026-09-04T03:45:00Z = 23:45 America/New_York on 2026-09-03 (EDT). Fixed so
#: the run is reproducible; the DST resolution itself is covered by the domain
#: suite, and pinning it here keeps the fault injection the only variable.
RESTART_NS = 1_788_493_500 * 1_000_000_000
NS = 1_000_000_000
LEAD_SECONDS = 60
WINDOW_SECONDS = 300


def _require_cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; the restart-window CLI cannot be built")
    return cargo


@pytest.fixture(scope="module")
def cli_path() -> Path:
    """Build the operator CLI once and return its path.

    Built rather than shelled through ``cargo run`` so each case measures the
    binary an operator would run, not a build step.
    """
    cargo = _require_cargo()
    result = subprocess.run(
        [cargo, "build", "-p", "atp-orchestrator", "--bin", CLI_BIN],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"building {CLI_BIN} failed:\n{result.stderr}"
    path = ROOT / "target" / "debug" / CLI_BIN
    assert path.is_file(), f"{path} was not produced by the build"
    return path


def dead_loopback_port() -> int:
    """A loopback port with nothing listening, right now.

    Bind, read the assigned number, release. There is an unavoidable race — the
    OS may hand the port to someone else before the probe runs — but the
    ephemeral range is large and the window is microseconds. If the port DID get
    reused the test would fail loudly (the gateway would read as reachable),
    never pass for the wrong reason.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert port not in (4001, 4002), "the fault injection must never target the IB ports"
    return port


def run_cli(cli_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(cli_path),
            *args,
            "--restart-ns",
            str(RESTART_NS),
            "--lead-seconds",
            str(LEAD_SECONDS),
            "--window-seconds",
            str(WINDOW_SECONDS),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def field(stdout: str, prefix: str, key: str) -> str:
    line = next(
        (line for line in stdout.splitlines() if line.startswith(prefix)),
        None,
    )
    assert line is not None, f"output has no `{prefix}` line:\n{stdout}"
    needle = f"{key}:"
    token = next(
        (tok for tok in line.split() if tok.startswith(needle)),
        None,
    )
    assert token is not None, f"`{prefix}` line has no `{key}`:\n{line}"
    return token[len(needle) :]


def assert_proved(result: subprocess.CompletedProcess[str], sentinel: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"expected exit 0:\n{combined}"
    assert sentinel in result.stdout.splitlines(), f"missing `{sentinel}`:\n{combined}"


def test_the_fault_is_a_genuinely_dead_port() -> None:
    """The premise, checked before anything is derived from it.

    A test whose fault never armed would report the gateway as unreachable for
    the wrong reason, and every assertion below would pass while proving
    nothing.
    """
    port = dead_loopback_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(2.0)
        with pytest.raises((ConnectionRefusedError, OSError)):
            probe.connect(("127.0.0.1", port))

    # The non-vacuity partner: a port that IS listening must connect, or the
    # check above would pass on a machine where loopback is simply broken.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        live_port = listener.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(2.0)
            probe.connect(("127.0.0.1", live_port))


def test_before_the_lead_no_proof_claims_a_phase_it_is_not_in(cli_path: Path) -> None:
    """Step 1 — the baseline, and a control on the proofs themselves.

    Well before the lead the window is doing nothing, so none of the three
    proofs is derivable here: each asserts the SYS-75 phase it names, precisely
    so a proof line cannot outrun what it proved. The healthy-routing positive
    control lives at step 4, inside the window, where SYS-75(c)/(d) actually
    applies.
    """
    for subcommand in ("prove-suspension", "prove-escalation", "prove-resume"):
        result = run_cli(
            cli_path,
            subcommand,
            "--now-ns",
            str(RESTART_NS - (LEAD_SECONDS + 600) * NS),
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, f"{subcommand} must not prove anything here:\n{combined}"
        assert "-proven:true" not in combined, combined
        assert field(result.stdout, "window ", "phase") == "Normal"


def test_inside_the_lead_orders_and_market_data_are_suspended(cli_path: Path) -> None:
    """Step 2 — SyRS SYS-75(a)+(b), against a dead port."""
    result = run_cli(
        cli_path,
        "prove-suspension",
        "--now-ns",
        str(RESTART_NS - 30 * NS),
    )
    assert_proved(result, "restart-window-suspension-proven:true")
    assert field(result.stdout, "gateway ", "state") == "ScheduledRestartWindow"
    assert field(result.stdout, "gateway ", "scheduled_restart") == "true"
    assert field(result.stdout, "witness ", "ib-orders-created") == "0"
    assert field(result.stdout, "market-data ", "admitted") == "false"
    assert field(result.stdout, "alerts ", "disposition") == "SUPPRESSED"
    assert field(result.stdout, "alerts ", "messages-sent") == "0"
    # The mutating admission point, in the evidence the feature's
    # verification_method rests on rather than only at the Rust layer.
    assert field(result.stdout, "registry ", "lines-opened") == "0"
    assert field(result.stdout, "registry ", "refusal") == "SuspendedForScheduledRestart"


def test_inside_the_window_a_dead_gateway_stays_suppressed(cli_path: Path) -> None:
    """Step 3 — the restart is under way. This is the case a naive
    implementation gets right and the next one wrong."""
    result = run_cli(
        cli_path,
        "prove-escalation",
        "--inject",
        "inside-window",
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"escalation must not be derivable mid-window:\n{combined}"
    assert field(result.stdout, "gateway ", "state") == "ScheduledRestartWindow"
    assert field(result.stdout, "alerts ", "disposition") == "SUPPRESSED"


def test_inside_the_window_a_returning_gateway_resumes(cli_path: Path) -> None:
    """Step 4 — SyRS SYS-75(c)+(d)."""
    result = run_cli(
        cli_path,
        "prove-resume",
        "--now-ns",
        str(RESTART_NS + 120 * NS),
    )
    assert_proved(result, "restart-window-resume-proven:true")
    assert field(result.stdout, "gateway ", "reachability") == "REACHABLE"
    assert field(result.stdout, "witness ", "ib-orders-created") == "1"
    # The positive control for step 2's zero.
    assert field(result.stdout, "registry ", "lines-opened") == "1"


def test_after_the_window_a_dead_gateway_escalates_and_pages(cli_path: Path) -> None:
    """Step 5 — the clause the whole requirement turns on.

    A window that never closed would suppress a real failure indefinitely, and
    steps 1-4 would all still pass. This is the only case that catches it.
    """
    result = run_cli(
        cli_path,
        "prove-escalation",
        "--now-ns",
        str(RESTART_NS + (WINDOW_SECONDS + 1) * NS),
    )
    assert_proved(result, "restart-window-escalation-proven:true")
    assert field(result.stdout, "window ", "phase") == "Elapsed"
    assert field(result.stdout, "gateway ", "state") == "Unreachable"
    assert field(result.stdout, "gateway ", "scheduled_restart") == "false", (
        "the maintenance marker is what silences the page; it must be gone"
    )
    assert field(result.stdout, "alerts ", "disposition") == "DISPATCHED"
    assert int(field(result.stdout, "alerts ", "messages-sent")) > 0
    assert field(result.stdout, "market-data ", "admission") == "CONNECTIVITY_LOST", (
        "after the window the refusal is an outage, not maintenance"
    )


def test_the_escalation_boundary_is_exact_to_the_nanosecond(cli_path: Path) -> None:
    """One nanosecond before the window closes the same dead gateway is still
    planned maintenance; at the boundary it is an outage.

    Testing the pair rather than one side is what makes this a boundary rather
    than a sample.
    """
    just_inside = run_cli(
        cli_path,
        "prove-escalation",
        "--now-ns",
        str(RESTART_NS + WINDOW_SECONDS * NS - 1),
    )
    assert just_inside.returncode != 0, just_inside.stdout
    assert field(just_inside.stdout, "gateway ", "state") == "ScheduledRestartWindow"

    at_boundary = run_cli(
        cli_path,
        "prove-escalation",
        "--now-ns",
        str(RESTART_NS + WINDOW_SECONDS * NS),
    )
    assert_proved(at_boundary, "restart-window-escalation-proven:true")
    assert field(at_boundary.stdout, "gateway ", "state") == "Unreachable"


def test_a_widened_window_delays_the_escalation(cli_path: Path) -> None:
    """SyRS SYS-75 calls the window configurable. An instant that escalates
    under the 5-minute default must stay suppressed under a longer one, or the
    configuration is decorative."""
    now_ns = RESTART_NS + (WINDOW_SECONDS + 1) * NS
    default_window = run_cli(cli_path, "prove-escalation", "--now-ns", str(now_ns))
    assert_proved(default_window, "restart-window-escalation-proven:true")

    widened = subprocess.run(
        [
            str(cli_path),
            "prove-escalation",
            "--now-ns",
            str(now_ns),
            "--restart-ns",
            str(RESTART_NS),
            "--lead-seconds",
            str(LEAD_SECONDS),
            "--window-seconds",
            "900",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert widened.returncode != 0, "a 15-minute window must still be suppressing here"
    assert field(widened.stdout, "gateway ", "state") == "ScheduledRestartWindow"
