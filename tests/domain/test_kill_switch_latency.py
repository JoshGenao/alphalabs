"""SRS-SAFE-001 / NFR-P3 — kill switch must complete within 5 seconds.

Drives the kill switch exactly the way an operator does — through the
operator runtime's CLI dispatcher (``kill-switch activate --confirm``) with
the production :class:`atp_safety.RustCliKillSwitchBackend` shelling the
cargo-built ``safe001_kill_switch_cli`` — over the NFR-SC1 reference shape:
**50 open positions**, 50 resting orders, 30 paper engines, mocked-IB fixture
transport (the transport the feature's own verification Step 2 prescribes;
the live IB transport is the deferred SRS-EXE-006 adapter).

Asserts, per the SYS-44a / NFR-P3 measurement definition:

* end-to-end operator wall time ≤ 5.0 s;
* the gate's own ``liquidations_submitted_ms`` mark (activation →
  confirmation that all cancels are done and all liquidation orders are
  submitted, on the injected monotonic clock) ≤ 5 000 ms;
* every one of the 50 positions has a corresponding liquidation order;
* the perf mode's nearest-rank percentile run over repeated fresh activations
  reports ``verdict:PASS`` on the same rule.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from atp_logging import LogClass
from atp_logging.persistence import JsonlLogStore
from atp_runtime import OperatorInterfaceRuntime
from atp_safety import RustCliKillSwitchBackend, wire_kill_switch

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_BINARY = REPO_ROOT / "target" / "debug" / "safe001_kill_switch_cli"

REFERENCE_POSITIONS = 50
REFERENCE_RESTING = 50
REFERENCE_ENGINES = 30
NFR_P3_BUDGET_SECONDS = 5.0


def _build_cli() -> None:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot build the kill-switch CLI")
    build = subprocess.run(
        [cargo, "build", "-p", "atp-orchestrator", "--bin", "safe001_kill_switch_cli"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"CLI build failed:\n{build.stdout}\n{build.stderr}"


def test_kill_switch_completes_within_5_seconds(tmp_path: Path) -> None:
    _build_cli()
    runtime = OperatorInterfaceRuntime()
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    backend = RustCliKillSwitchBackend(
        CLI_BINARY,
        scenario_args=(
            "--positions",
            str(REFERENCE_POSITIONS),
            "--resting",
            str(REFERENCE_RESTING),
            "--engines",
            str(REFERENCE_ENGINES),
        ),
        timeout_s=NFR_P3_BUDGET_SECONDS + 5.0,
    )
    wire_kill_switch(runtime, backend=backend, system_log_store=store, state_dir=state_dir)
    cli = runtime.cli_dispatcher()

    out = io.StringIO()
    started = time.monotonic()
    exit_code = cli.dispatch(["kill-switch", "activate", "--confirm", "--json"], stdout=out)
    elapsed_seconds = time.monotonic() - started

    assert exit_code == 0, f"activation failed: {out.getvalue()}"
    assert elapsed_seconds <= NFR_P3_BUDGET_SECONDS, (
        f"operator-observed kill-switch time {elapsed_seconds:.3f}s exceeds the "
        f"{NFR_P3_BUDGET_SECONDS}s NFR-P3 budget"
    )

    body = json.loads(out.getvalue())
    liquidations = body["liquidation_orders"]
    assert len(liquidations) == REFERENCE_POSITIONS, (
        "every open position must have a corresponding liquidation order"
    )
    symbols = {entry["symbol"] for entry in liquidations}
    assert len(symbols) == REFERENCE_POSITIONS, "one liquidation per distinct position"
    assert all(
        entry["outcome"]["status"] == "SUCCEEDED" and entry["side"] in ("BUY", "SELL")
        for entry in liquidations
    )
    assert body["paper_engines_halted"] == REFERENCE_ENGINES
    assert body["ib_gateway_disconnected"] is True

    # The gate's own NFR-P3 mark (activation → all cancels + all liquidation
    # submissions) from the persisted report, via kill-switch status.
    status_out = io.StringIO()
    assert cli.dispatch(["kill-switch", "status", "--json"], stdout=status_out) == 0
    status_body = json.loads(status_out.getvalue())
    assert status_body["last_activation"]["within_nfr_p3"] is True


def test_kill_switch_perf_run_passes_the_nfr_p3_verdict() -> None:
    # Repeated fresh activations over the reference shape; the CLI's perf mode
    # reports nearest-rank p50/p95/p99/p99.9 and a PASS/FAIL verdict on
    # max liquidations_submitted_ms <= 5000 (kill_switch_activation_contract).
    _build_cli()
    result = subprocess.run(
        [str(CLI_BINARY), "perf", "--iterations", "20"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"perf run failed:\n{result.stdout}\n{result.stderr}"
    assert "verdict:PASS" in result.stdout
    assert "budget_ms:5000" in result.stdout
    assert (
        f"shape: positions:{REFERENCE_POSITIONS} resting:{REFERENCE_RESTING} "
        f"engines:{REFERENCE_ENGINES}" in result.stdout
    )
