"""SRS-EXE-001 / SyRS SYS-2a, SYS-2d, AC-15 — orders reach IB only for the designated live strategy.

L7 domain (safety) test for the live execution host. AGENTS.md's core constraint is
"exactly one strategy may execute against the IB live account at any time"; this is
the process that enforces it for every real order.

It drives the REAL binaries in fresh OS processes:

* ``exe001_live_designation_cli`` designates the live strategy (explicit
  confirmation required), writing the shared durable snapshot;
* ``live_execution_host`` serves one Unix socket per strategy, re-reads that
  snapshot on every order, and routes through ``ExecutionEngine::route_order_durably``.

The broker side is observed from OUTSIDE the host: the fixture tier appends one
line per order that reached its gateway to ``--fixture-wire-ledger``. So "the paper
strategies never reached IB" is a fact about the wire (``wire-attempts:0``), not
about the host's own replies.

Deliberately NOT shelling ``cargo test``: a harness that shells another test proves
nothing unless it also proves the inner test asserts (docs/playbooks/test-integrity.md
r4/r5). Driving the binaries and parsing their real output removes that question.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST_BIN = "live_execution_host"
DESIGNATE_BIN = "exe001_live_designation_cli"
LATENCY_BIN = "nfr_p1_ack_cli"
MAGIC = "ATP-LIVE-HOST/1"

LIVE = "live-a"
PAPER = [f"paper-{n}" for n in range(1, 6)]


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not on PATH; cannot build the live execution host")
    build = subprocess.run(
        [
            cargo,
            "build",
            "-p",
            "atp-orchestrator",
            "--bin",
            HOST_BIN,
            "--bin",
            DESIGNATE_BIN,
            "-p",
            "atp-types",
            "--bin",
            LATENCY_BIN,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"cargo build failed:\n{build.stderr}"
    paths = {
        name: REPO_ROOT / "target" / "debug" / name
        for name in (HOST_BIN, DESIGNATE_BIN, LATENCY_BIN)
    }
    for name, path in paths.items():
        assert path.exists(), f"{name} was not built at {path}"
    return paths


@pytest.fixture()
def root() -> Iterator[Path]:
    # Unix socket paths are limited to ~104 bytes, and pytest's tmp_path is deep.
    path = Path(tempfile.mkdtemp(prefix="exe001-"))
    (path / "s").mkdir(mode=0o700)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _designate(binaries, state: Path, strategy: str, confirm: str | None):
    argv = [str(binaries[DESIGNATE_BIN]), "promote", "--state", str(state), "--strategy", strategy]
    if confirm is not None:
        argv += ["--confirm", confirm]
    return subprocess.run(argv, capture_output=True, text=True)


class Host:
    """One running ``live_execution_host`` process."""

    def __init__(self, binaries, root: Path, strategies: list[str], *, init: bool) -> None:
        self.root = root
        self.ledger = root / "wire-ledger"
        argv = [
            str(binaries[HOST_BIN]),
            "serve",
            "--designation-state",
            str(root / "designation"),
            "--outbox",
            str(root / "outbox"),
            "--socket-dir",
            str(root / "s"),
            "--transport",
            "fixture",
            "--fixture-wire-ledger",
            str(self.ledger),
        ]
        if init:
            argv.append("--outbox-init")
        for strategy in strategies:
            argv += ["--strategy", strategy]
        self.process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        assert self.process.stdout is not None
        ready = self.process.stdout.readline()
        if not ready.startswith("live-host-ready:true"):
            self.process.kill()
            _, err = self.process.communicate()
            raise AssertionError(f"host did not start: {ready!r}\n{err}")
        assert "\ttier=FIXTURE\t" in ready

    def submit(self, strategy: str, correlation_id: str) -> dict[str, str]:
        frame = (
            f"{MAGIC}\tsubmit\tcorrelation_id={correlation_id}\tsymbol=AAPL\tside=BUY"
            "\tquantity=1\tasset_class=EQUITY\torder_type=LIMIT\tlimit_price_minor=19000\n"
        )
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(10)
            conn.connect(str(self.root / "s" / strategy / "order.sock"))
            conn.sendall(frame.encode())
            reply = conn.makefile("r").readline()
        assert reply.endswith("\n"), f"unterminated reply {reply!r}"
        magic, outcome, *fields = reply.rstrip("\n").split("\t")
        assert magic == MAGIC
        parsed = dict(field.split("=", 1) for field in fields)
        parsed["outcome"] = outcome
        return parsed

    def wire(self) -> list[str]:
        """Which strategy each order that reached the gateway came from."""
        if not self.ledger.exists():
            return []
        return [line.split("\t")[1] for line in self.ledger.read_text().splitlines()]

    def stop(self) -> None:
        self.process.terminate()
        self.process.communicate(timeout=10)


@pytest.fixture()
def host_factory(binaries, root):
    started: list[Host] = []

    def make(strategies: list[str], *, init: bool = True) -> Host:
        host = Host(binaries, root, strategies, init=init)
        started.append(host)
        return host

    yield make
    for host in started:
        if host.process.poll() is None:
            host.stop()


def test_with_one_live_and_five_paper_strategies_only_the_live_one_reaches_ib(
    binaries, root, host_factory
):
    promoted = _designate(binaries, root / "designation", LIVE, "operator confirms live-a")
    assert promoted.returncode == 0, promoted.stderr
    assert "designated:live-a" in promoted.stdout
    host = host_factory([LIVE, *PAPER])

    for round_ in range(3):
        ack = host.submit(LIVE, f"live-{round_}")
        assert ack["outcome"] == "ack", ack
        assert ack["durable"] == "true"
        for paper in PAPER:
            reject = host.submit(paper, f"{paper}-{round_}")
            assert reject["outcome"] == "reject", reject
            assert reject["category"] == "NON_LIVE_STRATEGY_SUBMISSION"
            assert reject["error_type"] == "NotDesignatedLiveStrategy"
            assert reject["correlation_id"] == f"{paper}-{round_}"
            assert paper in reject["message"]

    # The wire's own record: three orders, all from the live strategy. Fifteen paper
    # attempts reached the gateway zero times.
    assert host.wire() == [LIVE, LIVE, LIVE]


def test_designation_requires_an_explicit_confirmation(binaries, root, host_factory):
    state = root / "designation"
    for confirm in (None, "", "   "):
        refused = _designate(binaries, state, LIVE, confirm)
        assert refused.returncode == 2, (confirm, refused.stdout)
    assert not state.exists(), "a refused promote wrote the designation snapshot"

    host = host_factory([LIVE])
    reply = host.submit(LIVE, "c-1")
    assert reply["category"] == "NON_LIVE_STRATEGY_SUBMISSION", reply
    assert host.wire() == []


def test_a_second_strategy_cannot_be_designated_while_one_is_live(binaries, root, host_factory):
    state = root / "designation"
    assert _designate(binaries, state, LIVE, "yes").returncode == 0
    before = state.read_bytes()
    second = _designate(binaries, state, PAPER[0], "yes")
    assert second.returncode == 2
    assert "already the designated live strategy" in second.stderr
    assert "Hot-Swap" in second.stderr
    assert state.read_bytes() == before

    host = host_factory([LIVE, PAPER[0]])
    assert host.submit(PAPER[0], "p-1")["outcome"] == "reject"
    assert host.wire() == []


def test_the_live_ib_tier_refuses_to_start_without_a_stale_data_producer(binaries, root):
    refused = subprocess.run(
        [
            str(binaries[HOST_BIN]),
            "serve",
            "--designation-state",
            str(root / "designation"),
            "--outbox",
            str(root / "outbox"),
            "--outbox-init",
            "--socket-dir",
            str(root / "s"),
            "--strategy",
            LIVE,
            "--transport",
            "ib",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert refused.returncode == 2
    assert "SRS-MD-004" in refused.stderr
    assert "live-host-ready" not in refused.stdout
    assert not (root / "outbox").exists(), "a refused tier created durable state"
    assert os.listdir(root / "s") == [], "a refused tier bound a socket"


def test_a_restart_keeps_the_designation_and_never_resubmits_a_replayed_order(
    binaries, root, host_factory
):
    assert _designate(binaries, root / "designation", LIVE, "yes").returncode == 0
    first = host_factory([LIVE, PAPER[0]])
    assert first.submit(LIVE, "c-1")["outcome"] == "ack"
    first.stop()

    second = host_factory([LIVE, PAPER[0]], init=False)
    replay = second.submit(LIVE, "c-1")
    assert replay["outcome"] == "reject", replay
    assert replay["category"] == "DUPLICATE_CLIENT_CORRELATION_ID"
    assert second.submit(LIVE, "c-2")["outcome"] == "ack"
    assert second.submit(PAPER[0], "p-1")["outcome"] == "reject"
    assert second.wire() == [LIVE, LIVE]


def test_an_unreadable_designation_refuses_every_order(binaries, root, host_factory):
    assert _designate(binaries, root / "designation", LIVE, "yes").returncode == 0
    host = host_factory([LIVE])
    (root / "designation").write_text("not a designation snapshot\n")
    reply = host.submit(LIVE, "c-1")
    assert reply["outcome"] == "refused", reply
    assert reply["error_type"] == "DesignationUnreadable"
    assert host.wire() == []


def test_the_host_refuses_a_socket_directory_other_users_can_reach(binaries, root):
    """The socket is the strategy's identity, so who can reach it is a safety fact."""
    (root / "s").chmod(0o755)
    refused = subprocess.run(
        [
            str(binaries[HOST_BIN]),
            "serve",
            "--designation-state",
            str(root / "designation"),
            "--outbox",
            str(root / "outbox"),
            "--outbox-init",
            "--socket-dir",
            str(root / "s"),
            "--strategy",
            LIVE,
            "--transport",
            "fixture",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert refused.returncode == 2, refused.stdout
    assert "mode 0755" in refused.stderr
    assert not (root / "s" / LIVE).exists()


def test_each_strategy_socket_is_private_to_its_own_directory(binaries, root, host_factory):
    host_factory([LIVE, *PAPER])
    for strategy in [LIVE, *PAPER]:
        directory = root / "s" / strategy
        assert directory.stat().st_mode & 0o777 == 0o700, strategy
        assert sorted(p.name for p in directory.iterdir()) == ["order.sock"], strategy
        assert (directory / "order.sock").lstat().st_mode & 0o777 == 0o600, strategy


# --------------------------------------------------------------------------- #
# End to end from the Python Strategy API (landing 2)
# --------------------------------------------------------------------------- #


class _RecordingStrategy:
    def __init__(self) -> None:
        self.events: list = []

    def on_order_event(self, context, event) -> None:
        self.events.append(event)


def _router(root: Path, strategy_id: str):
    from atp_strategy.api import AssetClass, StrategyConfig
    from atp_strategy.live_client import LiveHostClient, LiveOrderRouter, socket_path
    from atp_strategy.warmup import WarmupState

    strategy = _RecordingStrategy()
    client = LiveHostClient(socket_path(root / "s", strategy_id), reply_timeout_s=10)
    router = LiveOrderRouter(
        client=client,
        strategy=strategy,
        context=None,
        config=StrategyConfig(strategy_id, AssetClass.EQUITY),
        warmup_state=lambda: WarmupState.COMPLETE,
    )
    return router, strategy, client


def _limit_order():
    from atp_strategy.api import OrderRequest, OrderSide, OrderType

    return OrderRequest("AAPL", 1, OrderSide.BUY, OrderType.LIMIT, limit_price=190.25)


def _promote_through_the_operator_runtime(binaries, state: Path, strategy_id: str) -> dict:
    """Designate through the SHIPPED REST handler, not the binary directly."""

    from atp_orchestration import mount_live_designation
    from atp_runtime import OperatorInterfaceRuntime

    runtime = OperatorInterfaceRuntime()
    mount_live_designation(runtime, state_path=state, binary=binaries[DESIGNATE_BIN])
    unconfirmed = runtime.dispatch_rest("POST", f"/api/v1/strategies/{strategy_id}/promote-live")
    assert unconfirmed[0] == 428, unconfirmed
    assert not state.exists(), "an unconfirmed promote wrote the designation"
    status, body = runtime.dispatch_rest(
        "POST", f"/api/v1/strategies/{strategy_id}/promote-live?confirm=true"
    )
    assert status == 200, body
    return body


def test_python_strategies_one_live_five_paper_end_to_end(binaries, root, host_factory):
    """The AC through the Strategy API: operator confirms, host routes, callbacks arrive."""

    from atp_strategy.api import OrderEventType

    promoted = _promote_through_the_operator_runtime(binaries, root / "designation", LIVE)
    assert promoted["is_live"] is True and promoted["promoted_at"]
    host = host_factory([LIVE, *PAPER])
    routers = {sid: _router(root, sid) for sid in [LIVE, *PAPER]}
    try:
        for _ in range(3):
            for router, _strategy, _client in routers.values():
                router.order(_limit_order())
        for router, _strategy, _client in routers.values():
            assert router.deliver_pending() == 3

        live_events = routers[LIVE][1].events
        assert [e.event_type for e in live_events] == [OrderEventType.ACK] * 3
        assert all(e.order_id.startswith("IB-") for e in live_events), live_events
        for paper in PAPER:
            events = routers[paper][1].events
            assert [e.event_type for e in events] == [OrderEventType.REJECTED] * 3, paper
            assert all(
                e.reason.startswith("NON_LIVE_STRATEGY_SUBMISSION/NotDesignatedLiveStrategy")
                for e in events
            ), events
        # The wire's own record: three live orders, zero paper orders.
        assert host.wire() == [LIVE, LIVE, LIVE]
    finally:
        for _, _, client in routers.values():
            client.close()


def test_live_ack_latency_p95_is_under_one_second_on_the_host_clock(binaries, root, host_factory):
    """NFR-P1, MVP form: strategy API invocation -> acknowledgement callback, p95.

    Measured on the FIXTURE tier and labelled so by the verdict line: this proves the
    strategy-side and host path. The IB leg (a real broker acknowledgement) is the
    operator's live-ib run.
    """

    _promote_through_the_operator_runtime(binaries, root / "designation", LIVE)
    host_factory([LIVE, PAPER[0]])
    router, strategy, client = _router(root, LIVE)
    try:
        for _ in range(200):
            router.order(_limit_order())
            router.deliver_pending()
    finally:
        client.close()
    samples = router.latency_samples_ns
    assert len(samples) == 200 and len(strategy.events) == 200
    verdict = subprocess.run(
        [str(binaries[LATENCY_BIN]), "--tier", "FIXTURE"],
        input="\n".join(str(sample) for sample in samples),
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines = [line for line in verdict.stdout.splitlines() if line.startswith("nfr:")]
    assert len(lines) == 1, verdict.stdout + verdict.stderr
    assert "nfr:NFR-P1 clock:host-monotonic tier:FIXTURE samples:200" in lines[0]
    assert lines[0].endswith("verdict:PASS"), lines[0]
    assert verdict.returncode == 0


def test_a_designation_made_with_the_shipped_cli_is_what_the_host_routes_on(
    binaries, root, host_factory
):
    """Operator command to wire: `python -m atp_orchestration live promote` designates,
    and the host's very next orders route for that strategy and no other."""

    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("ATP_")}
    env["PYTHONPATH"] = str(REPO_ROOT / "python")
    env["ATP_HOT_SWAP_DESIGNATION_STATE"] = str(root / "designation")
    env["ATP_LIVE_DESIGNATION_BINARY"] = str(binaries[DESIGNATE_BIN])
    promoted = subprocess.run(
        [sys.executable, "-m", "atp_orchestration", "live", "promote", PAPER[0], "--confirm"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert promoted.returncode == 0, promoted.stdout + promoted.stderr

    host = host_factory([LIVE, PAPER[0]])
    assert host.submit(PAPER[0], "p-1")["outcome"] == "ack"
    rejected = host.submit(LIVE, "l-1")
    assert rejected["category"] == "NON_LIVE_STRATEGY_SUBMISSION", rejected
    assert host.wire() == [PAPER[0]]
