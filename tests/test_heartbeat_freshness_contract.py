"""Contract tests for SRS-MD-003 (continuous heartbeat freshness monitoring).

SRS-MD-003 / SyRS SYS-39 / NFR-P5 / StRS SN-2.03.

Pins the cross-language surface so it cannot silently drift:

* ``architecture/runtime_services.json`` carries the
  ``heartbeat_freshness_contract`` block with the strict OVER-15-seconds
  boundary wording and the NFR-P5 constant name;
* the Python bridge's ``THRESHOLD_MS`` mirrors the Rust authority
  ``atp_types::HEARTBEAT_STALENESS_THRESHOLD_MS`` (literal source anchor —
  the two constants live on opposite sides of a subprocess boundary and
  MUST NOT diverge);
* the monitor / sink / CLI named by the contract exist in the market-data
  crate, and the strict-boundary predicate keeps its strictly-greater form;
* the HEARTBEAT_STALE / HEARTBEAT_RECOVERED taxonomy additions exist under
  BOTH log sources, in records.py and in the JSON contract (belt-and-braces
  over the exact-equality tests in test_log_record_contract.py);
* the critic-required L7 domain test file exists and shells the pinned Rust
  integration-test target.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from atp_dashboard.heartbeat import THRESHOLD_MS

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((REPO_ROOT / "architecture" / "runtime_services.json").read_text())
MD_LIB = (REPO_ROOT / "crates" / "atp-market-data" / "src" / "lib.rs").read_text()
TYPES_PERF = (REPO_ROOT / "crates" / "atp-types" / "src" / "perf.rs").read_text()


class HeartbeatFreshnessContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.block = CONTRACT["heartbeat_freshness_contract"]

    def test_contract_block_pins_requirement_and_threshold(self) -> None:
        self.assertEqual(self.block["requirement"], "SRS-MD-003")
        self.assertIn("SYS-39", self.block["syrs_refs"])
        self.assertIn("NFR-P5", self.block["syrs_refs"])
        threshold = self.block["threshold"]
        self.assertEqual(threshold["constant"], "HEARTBEAT_STALENESS_THRESHOLD_MS")
        self.assertEqual(threshold["value_ms"], 15_000)
        self.assertIn(
            "STRICTLY greater than 15 000 ms",
            threshold["boundary"],
            "the AC's OVER-15-seconds boundary must stay strict",
        )
        self.assertIn("exactly 15 000 ms is Fresh", threshold["boundary"])

    def test_python_threshold_mirrors_the_rust_authority(self) -> None:
        self.assertEqual(THRESHOLD_MS, 15_000)
        self.assertIn(
            "pub const HEARTBEAT_STALENESS_THRESHOLD_MS: u64 = 15_000;",
            TYPES_PERF,
            "the Rust NFR-P5 authority constant moved or changed value — the "
            "Python bridge mirror (atp_dashboard.heartbeat.THRESHOLD_MS) and "
            "this contract must be updated in lockstep",
        )
        self.assertEqual(THRESHOLD_MS, self.block["threshold"]["value_ms"])

    def test_monitor_surface_exists_with_strict_boundary_predicate(self) -> None:
        for name in (
            "pub struct HeartbeatFreshnessMonitor",
            "pub trait HeartbeatEventSink",
            "pub struct HeartbeatStatus",
            "pub fn combined_line_freshness(",
            "pub fn observe_broker_heartbeat(",
        ):
            self.assertIn(name, MD_LIB, f"contract-named surface {name!r} missing")
        # THE boundary, in its strictly-greater form (a >= here would detect
        # exactly-15s as stale, violating the AC's OVER wording).
        self.assertIn(
            "age_ns > HEARTBEAT_STALENESS_THRESHOLD_MS.saturating_mul(1_000_000)",
            MD_LIB,
        )
        cli = REPO_ROOT / "crates" / "atp-market-data" / "src" / "bin" / "md003_heartbeat_cli.rs"
        self.assertTrue(cli.is_file(), "contract-named CLI binary source missing")

    def test_log_taxonomy_carries_heartbeat_transitions_under_both_sources(self) -> None:
        from atp_logging.records import EVENT_TYPES_BY_SOURCE, Source

        for source in (Source.MARKET_DATA, Source.IB_GATEWAY):
            for event_type in ("HEARTBEAT_STALE", "HEARTBEAT_RECOVERED"):
                self.assertIn(event_type, EVENT_TYPES_BY_SOURCE[source])
        json_taxonomy = CONTRACT["log_record_contract"]["event_types_by_source"]
        for source_key in ("market_data", "ib_gateway"):
            for event_type in ("HEARTBEAT_STALE", "HEARTBEAT_RECOVERED"):
                self.assertIn(event_type, json_taxonomy[source_key])

    def test_domain_test_shells_the_pinned_rust_target(self) -> None:
        domain_test = REPO_ROOT / self.block["domain_test"]
        self.assertTrue(domain_test.is_file(), "critic-required domain test missing")
        body = domain_test.read_text()
        self.assertIn(self.block["rust_integration_test"], body)
        self.assertIn("pytest.mark.safety", body)
        rust_test = (
            REPO_ROOT
            / "crates"
            / "atp-market-data"
            / "tests"
            / f"{self.block['rust_integration_test']}.rs"
        )
        self.assertTrue(rust_test.is_file())


if __name__ == "__main__":
    unittest.main()
