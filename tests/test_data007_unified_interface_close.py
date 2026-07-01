"""SRS-DATA-007 CLOSE — the consolidating four-consumer contract test.

SRS-DATA-007 (docs/SRS.md line 177; SyRS SYS-27 / SYS-53; StRS SN-1.28 / SN-3.03 / BG-5):
"The software shall provide a unified historical data access interface." Acceptance criterion:
"**Strategy code, backtests, factor jobs, and notebooks query by symbol, date range, and resolution
WITHOUT specifying the original source provider.**" Verification method (SRS): **Contract test**.

The four named consumers were wired over sessions 70–75; the per-consumer behavioural proofs live in
``tests/test_unified_query_contract.py`` (engine + CLI), ``tests/test_store_history_contract.py`` +
``tests/domain/test_store_history_consumer.py`` (the Python binding = strategy + notebook),
``crates/atp-simulation/tests/srs_data_007_store_bar_source.rs`` (backtest), and
``crates/atp-factor-pipeline/tests/srs_data_007_store_market_inputs.rs`` + ``srs_fac_001_store_backed_job.rs``
(factor jobs). THIS test is the single legible artifact that maps the AC's four named consumers to their
concrete query surfaces and asserts each reads the *provider-neutral* unified path — the contract the close
rests on — so no one consumer surface can be removed/renamed without re-examining the close.

Scope boundary (the reason this closes while SRS-RES-002 stays open): the SRS specifies the Jupyter
notebook **HOST** runtime *separately* as SRS-RES-002 (docs/SRS.md line 209, verification "Test,
demonstration" — kernel / indicators / plotting / no-live-order isolation). DATA-007 is the data-access
*interface* (verified by a contract test); a notebook *querying* that interface is plain Python importing
the same binding every consumer uses. The notebook DATA ACCESS is wired and tested here; the Jupyter
HOST that runs the notebook is RES-002's separate concern. This test pins that distinction against the
SRS text itself, so the close rationale is explicit and self-grounding.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"
PYTHON_ROOT = ROOT / "python"
for p in (TOOLS_ROOT, PYTHON_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import store_history_check as sh  # noqa: E402
import unified_query_check as uq  # noqa: E402

STORE_BAR_SOURCE = ROOT / "crates" / "atp-simulation" / "src" / "store_bar_source.rs"
SIM_LIB = ROOT / "crates" / "atp-simulation" / "src" / "lib.rs"
STORE_INPUTS = ROOT / "crates" / "atp-factor-pipeline" / "src" / "store_inputs.rs"
SRS = ROOT / "docs" / "SRS.md"

_NAMED_CONSUMERS = ("strategy code", "backtests", "factor jobs", "notebooks")

# Stale "not closed yet" prose that must NOT survive in a DATA-007-owned contract block once the
# interface is closed. A passing close that left any of these behind would be public contract drift
# (the description claiming completion while another sentence still says it is unwired / reverted).
_STALE_NOT_COMPLETE = (
    "STAYS passes:false",
    "remaining unwired",
    "not yet WIRED",
    "not yet wired",
    "only a strategy stand-in",
    "strategy stand-in",
    "the flip to passes:true was reverted",
    "flip was reverted",
    "The close is NOT complete",
    "the close is not complete",
    # The explicit-[start,end] method on the runtime_checkable HistoricalData Protocol is an SRS-SDK-001
    # strategy-authoring-surface concern, NOT a DATA-007 close item (the date-range AC is met by every
    # consumer). A block that still calls it "part of the DATA-007 close" is stale.
    "part of the DATA-007 close",
    "part of the deferred DATA-007 close",
)


class EngineAndBindingAreProviderNeutral(unittest.TestCase):
    """The unified ENGINE and the Python BINDING structurally cannot name a provider."""

    def test_engine_static_contract_passes(self) -> None:
        # 12 structural guards: the query struct carries only symbol/resolution/range (+ optional
        # vendor-neutral kind) and NO provider field; the result struct names no origin; the CLI prints
        # no provider line; the query path carries no vendor SDK token.
        evidence = uq.assert_unified_query_static(uq.load_config(), ROOT)
        self.assertEqual(len(evidence), 12)
        joined = " ".join(evidence)
        self.assertIn("names NO origin provider", joined)
        self.assertIn("source-neutral", joined)

    def test_binding_static_contract_passes(self) -> None:
        # 12 structural guards on StoreBackedHistoricalData: no provider/source/vendor/feed parameter,
        # no origin field read, source-neutral signature.
        evidence = sh.assert_store_history_static(sh.load_config(), ROOT)
        self.assertEqual(len(evidence), 12)
        self.assertIn("no", " ".join(evidence))


class AllFourNamedConsumersQueryTheUnifiedPath(unittest.TestCase):
    """Each AC-named consumer has a concrete surface that reads the provider-neutral unified path."""

    def test_strategy_and_notebook_consumer_surface(self) -> None:
        # Strategy code AND notebooks both query via the one Python binding (a notebook is plain Python
        # importing it). Its signature names no provider — proven by the binding's source-neutral guard.
        sh.check_source_neutral_signature(
            sh.load_config(), sh.module_source(sh.load_config(), ROOT)
        )

    def test_backtest_consumer_surface(self) -> None:
        src = STORE_BAR_SOURCE.read_text()
        self.assertIn("pub struct StoreBarSource", src)
        self.assertIn(
            "impl BarSource for StoreBarSource", src
        )  # implements the engine's BarSource port
        # Reads the provider-neutral engine path (raw + coverage-gated split-adjusted), no provider arg.
        self.assertIn("query_unified", src)
        self.assertIn("query_split_adjusted", src)
        # The struct declares no origin field a backtest could branch on.
        self.assertIsNone(
            re.search(r"\n\s*(?:pub\s+)?(provider|vendor|source|feed)\s*:", src),
            "StoreBarSource must not declare a provider/vendor/source/feed field",
        )
        # Wired into the engine: BacktestDataSource::SystemData is served by this consumer.
        lib = SIM_LIB.read_text()
        self.assertIn("StoreBarSource", lib)
        self.assertIn("BacktestDataSource::SystemData", lib)

    def test_factor_job_consumer_surface(self) -> None:
        src = STORE_INPUTS.read_text()
        for fn in (
            "pub fn load_daily_market_input",
            "pub fn load_fundamental_input",
            "pub fn assemble_factor_inputs",
            "pub fn run_scheduled_factor_job_over_store",
        ):
            self.assertIn(fn, src, f"factor-job consumer must expose {fn!r}")
        # Reads the provider-neutral engine path (raw + point-in-time gated split-adjusted).
        self.assertIn("query_unified", src)
        self.assertIn("query_split_adjusted_as_of", src)
        # No provider/vendor/source argument is threaded into the loaders.
        self.assertNotIn("provider:", src)


class CloseMetadataFramesData007Complete(unittest.TestCase):
    """The DATA-007 contract blocks describe a COMPLETE feature (no leftover passes:false framing)."""

    def test_unified_query_block_is_complete(self) -> None:
        desc = uq.contract_block(uq.load_config())["description"]
        self.assertIn("is COMPLETE", desc)

    def test_binding_block_is_complete_and_names_four_consumers(self) -> None:
        block = sh.contract_block(sh.load_config())
        self.assertEqual(tuple(block["consumers"]), _NAMED_CONSUMERS)

    def test_data007_owned_blocks_carry_no_stale_not_complete_prose(self) -> None:
        # File-level sweep (not line-grep): EVERY string in a DATA-007-owned contract block must be free
        # of "not closed yet" wording, so a description can't claim completion in one sentence while
        # another still says the consumers are unwired / the flip was reverted.
        for block in (uq.contract_block(uq.load_config()), sh.contract_block(sh.load_config())):
            blob = json.dumps(block)
            for phrase in _STALE_NOT_COMPLETE:
                self.assertNotIn(
                    phrase,
                    blob,
                    f"stale not-complete prose {phrase!r} survived in a DATA-007 block",
                )

    def test_close_metadata_is_reconciled_with_the_feature_record(self) -> None:
        # The passes flag (feature_list.json) is the source of truth; it flips false->true ONLY at
        # integration (close_feature.py --verified, under the scheduler lock) -- this branch deliberately
        # never edits feature_list.json. So the two metadata sources must be RECONCILABLE, never
        # contradictory: if the feature record still says passes:false (the on-branch / pre-integrate
        # state), the close prose MUST acknowledge the flip is integrate-time; once it is passes:true
        # (post-integrate), the prose's completion claim agrees with the record. This invariant holds in
        # BOTH states, so the close metadata and the source of truth can never silently disagree.
        record = next(
            f
            for f in json.loads((ROOT / "feature_list.json").read_text())
            if f["id"] == "SRS-DATA-007"
        )
        desc = uq.contract_block(uq.load_config())["description"]
        if record["passes"] is False:
            self.assertIn("closes to passes:true at integration", desc)
        else:
            self.assertTrue(record["passes"] is True)
            self.assertIn("is COMPLETE", desc)


# Markers that, post-close, mean a reference still frames DATA-007 ITSELF as deferred / unbuilt / the
# owner-of-an-unbuilt-thing (vs. naming the COMPLETE interface a still-deferred consumer reads via).
# Prior sessions used "deferred SRS-DATA-007 <X>" as shorthand for "the historical data layer" across
# sibling features (benchmark/factor); closing DATA-007 makes all of those stale.
_STALE_DEFERRAL_MARKERS = (
    "deferred SRS-DATA-007",
    "owner: SRS-DATA-007",
    "owner SRS-DATA-007",
    "SRS-DATA-007 behind",
    "SRS-DATA-007 bar grid",
    "SRS-DATA-007 adapter",
    "SRS-DATA-007 resolver",
    "SRS-DATA-007 stored-data",
    "SRS-DATA-007 data layer",
    "SRS-DATA-007-backed",
    "records()/get() groundwork",
    "part of the DATA-007 close",
    # DATA-007 named as NOT closed / co-open with another feature (Codex R5: this survived in the
    # DATA-012 normalization block, not a DATA-007-owned one).
    "not close SRS-DATA-007",
    "close SRS-DATA-007 or",
    "SRS-DATA-007 or SRS-DATA-012",
)

# DATA-007 as the SUBJECT of a "stays passes:false" claim = the feature framed as still open (e.g. the
# R5 bug "does NOT close SRS-DATA-007 or SRS-DATA-012 (both STAY passes:false)"). Anchored on DATA-007
# FIRST so a valid "SRS-DATA-012 STAYS passes:false): SRS-DATA-007 is COMPLETE" does not match.
_DATA007_STILL_OPEN_RE = re.compile(
    r"SRS-DATA-007[^.]{0,45}?STAYS?\s+passes:false",
    re.IGNORECASE,
)


class NoStaleDeferralFramingAnywhere(unittest.TestCase):
    """Codex R4 lesson: a DATA-007 deferral left in a SIBLING block (DATA-016 / benchmark / factor)
    contradicts the close just as badly as one in a DATA-007-owned block. Sweep the whole surface:
    architecture metadata + every evidence script + the swept Rust source."""

    def _scanned_files(self) -> list[Path]:
        files = [ROOT / "architecture" / "runtime_services.json"]
        files += sorted((ROOT / "tools").glob("*_check.py"))
        files += [
            ROOT / "crates/atp-simulation/src/benchmark.rs",
            ROOT / "crates/atp-simulation/src/lib.rs",
            ROOT / "crates/atp-simulation/src/bin/bt009_store_cli.rs",
            ROOT / "crates/atp-simulation/src/bin/benchmark_comparison_cli.rs",
            ROOT / "crates/atp-simulation/tests/srs_bt_005_benchmark.rs",
            ROOT / "crates/atp-factor-pipeline/src/factor_job.rs",
            ROOT / "crates/atp-factor-pipeline/src/factor_analysis.rs",
        ]
        return [f for f in files if f.exists()]

    def test_no_file_frames_data007_as_deferred(self) -> None:
        offenders: list[str] = []
        for f in self._scanned_files():
            text = f.read_text()
            for marker in _STALE_DEFERRAL_MARKERS:
                if marker in text:
                    offenders.append(f"{f.relative_to(ROOT)} :: {marker!r}")
            for m in _DATA007_STILL_OPEN_RE.finditer(text):
                # This test file itself defines the marker literals; skip self-matches.
                if f.name != Path(__file__).name:
                    offenders.append(f"{f.relative_to(ROOT)} :: still-open-regex {m.group(0)!r}")
        self.assertEqual(
            offenders,
            [],
            "stale 'DATA-007 is deferred/open' framing survived the close:\n"
            + "\n".join(offenders),
        )


class NotebookDataAccessIsSeparateFromTheJupyterHost(unittest.TestCase):
    """Pin the scope boundary against the SRS text: DATA-007 = interface (contract test);
    RES-002 = Jupyter host (demonstration). The notebook DATA ACCESS is DATA-007; the HOST is RES-002."""

    def test_srs_specifies_data007_as_a_contract_tested_interface(self) -> None:
        rows = [ln for ln in SRS.read_text().splitlines() if "SRS-DATA-007" in ln and "|" in ln]
        self.assertTrue(rows, "SRS-DATA-007 row not found")
        row = rows[0]
        self.assertIn("unified historical data access interface", row)
        self.assertIn("Contract test", row)

    def test_srs_specifies_the_jupyter_host_separately_as_res002(self) -> None:
        rows = [ln for ln in SRS.read_text().splitlines() if "SRS-RES-002" in ln and "|" in ln]
        self.assertTrue(rows, "SRS-RES-002 row not found")
        row = rows[0]
        self.assertIn("Jupyter", row)
        # RES-002 is verified by demonstration (the running host), distinct from DATA-007's contract test.
        self.assertIn("demonstration", row.lower())

    def test_close_metadata_cites_the_res002_scope_boundary(self) -> None:
        desc = uq.contract_block(uq.load_config())["description"]
        self.assertIn("SRS-RES-002", desc)
        self.assertIn("SEPARATE", desc)

    def test_date_range_ac_is_met_and_explicit_range_protocol_method_is_sdk001(self) -> None:
        # DATA-007's AC says consumers query by "date range". That is met on every consumer: the unified
        # engine + backtest StoreBarSource + factor store_inputs + the binding's get_bars_range all take an
        # explicit [start, end] range, and the strategy Protocol's get_bars(lookback, end) is a bounded
        # date-window query. Promoting an explicit-[start, end] method onto the runtime_checkable
        # HistoricalData Protocol (the strategy-AUTHORING surface) is an SRS-SDK-001 concern, NOT a DATA-007
        # requirement -- the DATA-007-owned blocks must attribute it that way, never as a DATA-007 gap.
        for desc in (
            uq.contract_block(uq.load_config())["description"],
            sh.contract_block(sh.load_config())["description"],
        ):
            self.assertIn("date-range AC is", desc)
            self.assertIn("SRS-SDK-001", desc)
            self.assertNotIn("part of the DATA-007 close", desc)


class ConsumersQueryProviderNeutrallyEndToEnd(unittest.TestCase):
    """Behavioural capstone: ingest fixtures (≤ two providers' kinds), then read via the binding the
    strategy/notebook consumers use — by symbol/date/resolution, no provider, source-neutral bars."""

    @staticmethod
    def _cargo() -> str | None:
        return shutil.which("cargo")

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(args), cwd=ROOT, check=False, capture_output=True, text=True)

    def test_binding_consumer_reads_source_neutral_bars(self) -> None:
        import dataclasses

        cargo = self._cargo()
        if cargo is None:
            self.skipTest("cargo not on PATH")
        build = self._run(
            cargo,
            "build",
            "-q",
            "-p",
            "atp-data",
            "--bin",
            "data016_ingest_cli",
            "--bin",
            "data007_query_cli",
        )
        self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
        ingest = ROOT / "target" / "debug" / "data016_ingest_cli"
        query = ROOT / "target" / "debug" / "data007_query_cli"

        from atp_strategy import NormalizationMode
        from atp_strategy.store_history import StoreBackedHistoricalData

        seed, day = 1_700_000_000, 86_400
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(3):
                init = ["--init"] if i == 0 else []
                r = self._run(
                    str(ingest),
                    "ingest",
                    "--dir",
                    tmp,
                    "--kind",
                    "daily-equity-bar",
                    "--event-ts",
                    str(seed + i * day),
                    *init,
                )
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            history = StoreBackedHistoricalData(store_dir=tmp, query_binary=query)
            bars = history.get_bars_range(
                "AAPL",
                frequency="1d",
                start=datetime.fromtimestamp(seed, tz=timezone.utc),
                end=datetime.fromtimestamp(seed + 2 * day, tz=timezone.utc),
                normalization=NormalizationMode.RAW,
            )
            self.assertEqual(len(bars), 3)
            self.assertEqual({b.symbol for b in bars}, {"AAPL"})
            # The Bar carries no origin field a strategy / notebook could branch on.
            fields = {f.name for f in dataclasses.fields(bars[0])}
            self.assertFalse(fields & {"provider", "source", "vendor", "feed"})


if __name__ == "__main__":
    unittest.main()
