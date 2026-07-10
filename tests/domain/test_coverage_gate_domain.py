"""SRS-DATA-011 corporate-action adjustment + coverage — L7 domain test (the keystone safety invariant).

The acceptance: corporate actions are reflected in historical records so that "backtests spanning
corporate-action dates produce correct P&L calculations under the selected normalization mode." The
keystone safety property is: **adjusted history is served ONLY behind proven coverage** — a backtest /
strategy can never silently consume raw-as-adjusted bars. This domain test drives the real gate
(``data007_query_cli`` over a real ingested store, standing in for a backtest reading adjusted history)
and asserts:

  * COVERED — when the symbol's coverage frontier reaches the query end, the pre-split bar is re-quoted
    onto the split-comparable basis (a 10000 close under a 4-for-1 split reads 2500), and the
    fully-adjusted (splits AND dividends, SYS-29) read composes the dividend leg (2475 with a $1.00
    dividend ex before the split; volume never dividend-scaled), so a backtest sees a continuous
    adjusted series — correct P&L;
  * UNCOVERED — when coverage is absent OR does not reach the query end, EVERY adjusted read
    (split-adjusted / fully-adjusted / total-return, the SRS-DATA-012 modes) FAILS CLOSED (a structured
    error naming SRS-DATA-011), so a backtest is never handed raw bars dressed as adjusted;
  * LINEAGE — a query for the current symbol spans a rename (the predecessor's bars come back
    relabeled) and the symbol-change event is surfaced, so a backtest spanning the rename sees one
    continuous series plus the structural fact.

Plus a structural assertion that the gate is registered in the architecture metadata as a CLOSED
(passes:true) SRS-DATA-011 contract with its twelve static guards pinned. Builds the data CLIs on
demand (skips if cargo is unavailable, like the other cargo-driven domain tests). This is the paired
``tests/domain/`` diff for the safety-critical coverage paths (``crates/atp-data/src/coverage.rs``,
``data011_coverage_cli``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

pytestmark = pytest.mark.domain


def _cargo() -> str | None:
    return shutil.which("cargo")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=ROOT, check=False, capture_output=True, text=True)


def _build(cargo: str) -> tuple[Path, Path, Path]:
    build = _run(
        cargo,
        "build",
        "-q",
        "-p",
        "atp-data",
        "--bin",
        "data016_ingest_cli",
        "--bin",
        "data011_coverage_cli",
        "--bin",
        "data007_query_cli",
    )
    assert build.returncode == 0, build.stdout + build.stderr
    debug = ROOT / "target" / "debug"
    return debug / "data016_ingest_cli", debug / "data011_coverage_cli", debug / "data007_query_cli"


def _close(stdout: str) -> int | None:
    for line in stdout.splitlines():
        if line.startswith("record.0.field.close:"):
            return int(line.split(":", 1)[1])
    return None


def test_backtest_gets_adjusted_only_when_covered_else_fails_closed() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        # A pre-split daily bar (AAPL@100, close 10000) and a 4-for-1 split @200.
        assert (
            _run(
                str(ingest_bin),
                "ingest",
                "--dir",
                tmp,
                "--kind",
                "daily-equity-bar",
                "--event-ts",
                "100",
                "--init",
            ).returncode
            == 0
        )
        assert (
            _run(
                str(ingest_bin),
                "ingest",
                "--dir",
                tmp,
                "--kind",
                "corporate-action-split",
                "--event-ts",
                "200",
            ).returncode
            == 0
        )

        # A $1.00 dividend (100 minor) ex @150 -- the fully-adjusted (SYS-29) input.
        assert (
            _run(
                str(ingest_bin),
                "ingest",
                "--dir",
                tmp,
                "--kind",
                "corporate-action-dividend",
                "--event-ts",
                "150",
            ).returncode
            == 0
        )

        def adjusted(end: int, normalization: str) -> subprocess.CompletedProcess[str]:
            return _run(
                str(query_bin),
                "query",
                "--dir",
                tmp,
                "--symbol",
                "AAPL",
                "--resolution",
                "1d",
                "--start",
                "0",
                "--end",
                str(end),
                "--kind",
                "daily-equity-bar",
                "--normalization",
                normalization,
            )

        # (1) NO coverage yet -> a backtest reading ANY adjusted history FAILS CLOSED (never raw bars
        # dressed as adjusted). This is the keystone safety property.
        for mode in ("split-adjusted", "fully-adjusted", "total-return"):
            no_coverage = adjusted(100, mode)
            assert no_coverage.returncode != 0
            assert "SRS-DATA-011" in no_coverage.stderr

        # (2) Assert coverage through 200, then the COVERED reads re-quote the pre-split bar:
        # split-adjusted 10000 / 4 = 2500 (the dividend record is IGNORED by mode semantics);
        # fully-adjusted composes the dividend leg (10000 · (1·9900)/(4·10000) = 2475). A backtest
        # sees the adjusted series -> correct P&L under the selected normalization mode.
        assert (
            _run(
                str(coverage_bin),
                "assert-coverage",
                "--dir",
                tmp,
                "--symbol",
                "AAPL",
                "--through",
                "200",
            ).returncode
            == 0
        )
        covered = adjusted(100, "split-adjusted")
        assert covered.returncode == 0, covered.stderr
        assert _close(covered.stdout) == 2500
        assert "coverage_through:200" in covered.stdout
        fully = adjusted(100, "fully-adjusted")
        assert fully.returncode == 0, fully.stderr
        assert _close(fully.stdout) == 2475
        assert "coverage_through:200" in fully.stdout
        # Volume takes the SPLIT factor only -- a dividend never scales a share count.
        assert "record.0.field.volume:400000" in fully.stdout
        # total-return (SRS-DATA-012) is served behind the SAME coverage gate. At this pre-ex bar
        # (dividend ex @150 > the queried bar @100) no dividend is reinvested yet, so the served value
        # is the split-only 2500 -- correct mode semantics; the reinvested-forward P&L over a POST-ex
        # bar is the integration test's scenario.
        total = adjusted(100, "total-return")
        assert total.returncode == 0, total.stderr
        assert _close(total.stdout) == 2500
        assert "coverage_through:200" in total.stdout

        # (3) A query PAST the frontier (end 250 > 200) still FAILS CLOSED for EVERY adjusted mode:
        # partial coverage is not enough -- an action could exist in the uncovered tail (200, 250].
        for mode in ("split-adjusted", "fully-adjusted", "total-return"):
            past_frontier = adjusted(250, mode)
            assert past_frontier.returncode != 0
            assert "SRS-DATA-011" in past_frontier.stderr

        # (4) The RAW path is always available without coverage (the gate is split-adjusted-only): a
        # backtest can still read unadjusted bars explicitly.
        raw = _run(
            str(query_bin),
            "query",
            "--dir",
            tmp,
            "--symbol",
            "AAPL",
            "--resolution",
            "1d",
            "--start",
            "0",
            "--end",
            "100",
            "--kind",
            "daily-equity-bar",
            "--normalization",
            "raw",
        )
        assert raw.returncode == 0
        assert _close(raw.stdout) == 10000


def test_lineage_read_spans_a_rename_and_surfaces_the_event() -> None:
    # A backtest querying the CURRENT symbol across a rename must see one continuous series (the
    # predecessor's bars relabeled) plus the structural symbol-change event -- the SRS-DATA-011
    # "symbol changes are reflected" property, end to end over the real CLIs.
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        # An AAPL bar @100, then AAPL renamed to AAPLN @300 (both fixture batches).
        assert (
            _run(
                str(ingest_bin),
                "ingest",
                "--dir",
                tmp,
                "--kind",
                "daily-equity-bar",
                "--event-ts",
                "100",
                "--init",
            ).returncode
            == 0
        )
        assert (
            _run(
                str(ingest_bin),
                "ingest",
                "--dir",
                tmp,
                "--kind",
                "corporate-action-symbol-change",
                "--event-ts",
                "300",
            ).returncode
            == 0
        )
        # Coverage is asserted for the QUERIED (current) symbol -- its frontier governs the lineage.
        assert (
            _run(
                str(coverage_bin),
                "assert-coverage",
                "--dir",
                tmp,
                "--symbol",
                "AAPLN",
                "--through",
                "400",
            ).returncode
            == 0
        )
        result = _run(
            str(query_bin),
            "query",
            "--dir",
            tmp,
            "--symbol",
            "AAPLN",
            "--resolution",
            "1d",
            "--start",
            "0",
            "--end",
            "400",
            "--kind",
            "daily-equity-bar",
            "--normalization",
            "split-adjusted",
        )
        assert result.returncode == 0, result.stderr
        # The predecessor's bar comes back under the queried symbol (relabeled, values verbatim).
        assert "record.0.event_ts:100" in result.stdout
        assert _close(result.stdout) == 10000
        # The rename is surfaced as a structural event a P&L consumer can follow.
        assert "event.0.kind:symbol-change" in result.stdout
        assert "event.0.symbol:AAPL" in result.stdout
        assert "event.0.successor:AAPLN" in result.stdout
        assert "event.0.effective_ts:300" in result.stdout


def test_corporate_action_facts_pass_the_sys77_ingestion_validator_but_coverage_stays_refused() -> (
    None
):
    # The merged seam with SRS-DATA-013: the operator ingest CLI routes every record through the real
    # SYS-77 validator (Sys77RecordValidator). The four corporate-action FACT kinds are not in
    # SYS-77's OHLCV/option rule set — only the duplicate rule applies (their per-kind
    # self-consistency lives in store.rs::validate_record) — so a well-formed fact batch must be
    # ADMITTED and persisted, while corporate-action COVERAGE remains refused at parse (the single
    # trust-assertion write surface is data011_coverage_cli, never the provider-shaped ingest path).
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, _coverage_bin, _query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        for i, kind in enumerate(
            (
                "corporate-action-dividend",
                "corporate-action-delisting",
                "corporate-action-merger",
                "corporate-action-symbol-change",
            )
        ):
            args = [
                str(ingest_bin),
                "ingest",
                "--dir",
                tmp,
                "--kind",
                kind,
                "--event-ts",
                str(100 + i),
            ]
            if i == 0:
                args.append("--init")
            done = _run(*args)
            assert done.returncode == 0, f"{kind} must pass the SYS-77 validator: {done.stderr}"
            assert "inserted:1" in done.stdout, done.stdout
        inspect = _run(str(ingest_bin), "inspect", "--dir", tmp)
        assert inspect.returncode == 0
        for kind in (
            "corporate-action-dividend",
            "corporate-action-delisting",
            "corporate-action-merger",
            "corporate-action-symbol-change",
        ):
            assert f"{kind}:1" in inspect.stdout, inspect.stdout
        # The coverage TRUST kind is still refused on this surface (fail closed at parse).
        refused = _run(
            str(ingest_bin),
            "ingest",
            "--dir",
            tmp,
            "--kind",
            "corporate-action-coverage",
            "--event-ts",
            "200",
        )
        assert refused.returncode != 0
        assert "data011_coverage_cli" in refused.stderr


def test_coverage_gate_is_registered_closed() -> None:
    # Structural: the gate is registered in the architecture metadata as a CLOSED (passes:true)
    # SRS-DATA-011 contract with the twelve static guards pinned, so it cannot silently drift.
    from coverage_manifest_check import (
        assert_coverage_manifest_static,
        contract_block,
        load_config,
    )

    config = load_config()
    block = contract_block(config)
    assert block["passes"] is True
    assert block["requirement"] == "SRS-DATA-011"
    assert block["supported_action_types"] == [
        "split",
        "reverse-split",
        "dividend",
        "delisting",
        "merger",
        "symbol-change",
    ]
    assert block["deferred_action_types"] == []
    assert len(assert_coverage_manifest_static(config, ROOT)) == 12
