"""Contract tests for SRS-RESV-005 (SyRS SYS-49d / AC-14; StRS SN-1.25 / SN-1.30).

Mirrors ``tests/test_hot_swap_demotion_contract.py``: shells out to
``tools/hot_swap_promotion_check.py``, then exercises each per-check function
in-process with a MUTATED source.

The mutations are the point. Every guard in that script is a claim that some
regression would be caught, and a guard nobody has watched fail is not evidence —
it is a grep that happens to match today. So each check below is shown to catch
its own regression: a receipt that gains public fields, ``Clone``, ``Default`` or a
public constructor; a gate that becomes callable from outside; an entry point that
reaches the gate without minting; a dropped guard; a second designation write; a
position probe that moves after the write; a missing rollback; a mutator added to
a read-only port; a deleted compile_fail doctest; a REST timeout that no longer
outlasts the demotion it waits on; and a stale-deferral claim creeping back in.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from hot_swap_promotion_check import (  # noqa: E402
    HotSwapPromotionCheckError,
    check_compile_fail_doctests,
    check_entry_point_sequencing,
    check_no_stale_deferral,
    check_ordered_guards,
    check_ports_read_only,
    check_receipt_encapsulation,
    check_rest_surface,
    load_config,
    module_source,
)


class HotSwapPromotionScriptTest(unittest.TestCase):
    def test_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/hot_swap_promotion_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-RESV-005 PASS", result.stdout)
        for needle in (
            "DemotionReceipt: private fields",
            "sole constructor `mint` is pub(crate)",
            "`execute_hot_swap` is the sole public path",
            "resolve_demotion -> DemotionReceipt::mint -> `promote_after_demotion`",
            "ordered guards present",
            "exactly one `designate(` write",
            "positions probed before it",
            "drift rolled back after it",
            "observation-only",
            "compile_fail doctests",
            "served_by=SRS-RESV-005",
            "> 60s demotion timeout",
            "stale-deferral collector",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = module_source(self.config)

    def assertCaught(self, check, mutated: str, needle: str) -> None:
        with self.assertRaises(HotSwapPromotionCheckError) as ctx:
            check(self.config, mutated)
        self.assertIn(needle, str(ctx.exception))


class ReceiptEncapsulationTest(_Base):
    def test_baseline_passes(self) -> None:
        self.assertIn("private fields", check_receipt_encapsulation(self.config, self.src))

    def test_a_public_field_is_caught(self) -> None:
        mutated = self.src.replace(
            "pub struct DemotionReceipt {\n    demoting_strategy_id: StrategyId,",
            "pub struct DemotionReceipt {\n    pub demoting_strategy_id: StrategyId,",
            1,
        )
        self.assertNotEqual(mutated, self.src, "field mutation did not apply")
        self.assertCaught(check_receipt_encapsulation, mutated, "public field")

    def test_a_clone_derive_is_caught(self) -> None:
        mutated = self.src.replace(
            "#[derive(Debug, PartialEq, Eq)]\npub struct DemotionReceipt {",
            "#[derive(Debug, Clone, PartialEq, Eq)]\npub struct DemotionReceipt {",
            1,
        )
        self.assertNotEqual(mutated, self.src, "derive mutation did not apply")
        self.assertCaught(check_receipt_encapsulation, mutated, "Clone")

    def test_a_default_derive_is_caught(self) -> None:
        mutated = self.src.replace(
            "#[derive(Debug, PartialEq, Eq)]\npub struct DemotionReceipt {",
            "#[derive(Debug, Default, PartialEq, Eq)]\npub struct DemotionReceipt {",
            1,
        )
        self.assertNotEqual(mutated, self.src, "derive mutation did not apply")
        self.assertCaught(check_receipt_encapsulation, mutated, "Default")

    def test_a_public_constructor_is_caught(self) -> None:
        # Anchored on `impl DemotionReceipt`'s SPAN, not on the bare token.
        #
        # `pub(crate) fn mint(` was unique when this was written and stopped being so
        # the moment SRS-RESV-006 added `PendingCooldownWindow::mint` — at which point
        # `.replace(..., 1)` mutated whichever `impl` the file declares FIRST, the
        # check received an intact `DemotionReceipt`, and the test failed for the one
        # reason that looks most like the guard working. `test-integrity.md` rule 27,
        # arriving from a sibling feature rather than from an edit to this one.
        start = self.src.index("impl DemotionReceipt {")
        end = self.src.index("\n}", start)
        span = self.src[start:end]
        mutated = self.src.replace(span, span.replace("pub(crate) fn mint(", "pub fn mint("), 1)
        self.assertNotEqual(mutated, self.src, "visibility mutation did not apply")
        self.assertCaught(check_receipt_encapsulation, mutated, "pub(crate)")

    def test_a_mint_that_ignores_promotion_allowed_is_caught(self) -> None:
        mutated = self.src.replace(
            "        if !resolved.promotion_allowed {\n            return None;\n        }\n",
            "",
            1,
        )
        self.assertNotEqual(mutated, self.src, "fail-closed mutation did not apply")
        self.assertCaught(check_receipt_encapsulation, mutated, "promotion_allowed")


class EntryPointSequencingTest(_Base):
    def test_baseline_passes(self) -> None:
        self.assertIn("sole public path", check_entry_point_sequencing(self.config, self.src))

    def test_a_publicly_callable_gate_is_caught(self) -> None:
        mutated = self.src.replace(
            "    pub(crate) fn promote_after_demotion<Q, H, R>(",
            "    pub fn promote_after_demotion<Q, H, R>(",
            1,
        )
        self.assertNotEqual(mutated, self.src, "gate visibility mutation did not apply")
        self.assertCaught(check_entry_point_sequencing, mutated, "pub(crate)")

    def test_an_entry_point_that_skips_the_demotion_is_caught(self) -> None:
        mutated = self.src.replace("self.resolve_demotion(", "self.skip_demotion(", 1)
        self.assertNotEqual(mutated, self.src, "resolve_demotion mutation did not apply")
        self.assertCaught(check_entry_point_sequencing, mutated, "resolve_demotion")

    def test_a_receipt_taken_by_reference_is_caught(self) -> None:
        mutated = self.src.replace(
            "        receipt: DemotionReceipt,", "        receipt: &DemotionReceipt,", 1
        )
        self.assertNotEqual(mutated, self.src, "by-value mutation did not apply")
        self.assertCaught(check_entry_point_sequencing, mutated, "by reference")


class OrderedGuardsTest(_Base):
    def test_baseline_passes(self) -> None:
        self.assertIn("ordered guards present", check_ordered_guards(self.config, self.src))

    def test_a_dropped_guard_is_caught(self) -> None:
        mutated = self.src.replace(
            "HotSwapPromotionError::PositionsOpen { symbols: held }", "todo!()", 1
        )
        self.assertNotEqual(mutated, self.src, "guard mutation did not apply")
        self.assertCaught(check_ordered_guards, mutated, "PositionsOpen")

    def test_a_second_designation_write_is_caught(self) -> None:
        mutated = self.src.replace(
            "        designation\n            .designate(requested_candidate.clone(), confirmation)",
            "        let _ = designation.designate(requested_candidate.clone(), confirmation);\n"
            "        designation\n            .designate(requested_candidate.clone(), confirmation)",
            1,
        )
        self.assertNotEqual(mutated, self.src, "second-write mutation did not apply")
        self.assertCaught(check_ordered_guards, mutated, "2 `designate(` calls")

    def test_a_position_probe_after_the_write_is_caught(self) -> None:
        # Move the probe read to the very end of the gate: still present, but no
        # longer gating the designation — the exact regression the order pins.
        probe_call = "positions.open_positions().map_err(|error| {"
        self.assertIn(probe_call, self.src)
        mutated = self.src.replace("open_positions()", "open_positions_LATE()", 1)
        mutated = mutated.replace(
            "        Ok(HotSwapPromoted {",
            "        let _ = positions.open_positions();\n        Ok(HotSwapPromoted {",
            1,
        )
        self.assertNotEqual(mutated, self.src, "probe-order mutation did not apply")
        self.assertCaught(check_ordered_guards, mutated, "AFTER designating")

    def test_a_missing_rollback_is_caught(self) -> None:
        mutated = self.src.replace("rollback(", "no_rollback(")
        self.assertNotEqual(mutated, self.src, "rollback mutation did not apply")
        self.assertCaught(check_ordered_guards, mutated, "rollback")


class PortsReadOnlyTest(_Base):
    def test_baseline_passes(self) -> None:
        self.assertIn("observation-only", check_ports_read_only(self.config, self.src))

    def test_a_mutator_on_a_read_only_port_is_caught(self) -> None:
        mutated = self.src.replace(
            "pub trait LivePositionProbe {\n    fn open_positions(",
            "pub trait LivePositionProbe {\n    fn promote_now(&self);\n    fn open_positions(",
            1,
        )
        self.assertNotEqual(mutated, self.src, "port mutation did not apply")
        self.assertCaught(check_ports_read_only, mutated, "LivePositionProbe")


class CompileFailDoctestTest(_Base):
    def test_baseline_passes(self) -> None:
        self.assertIn("compile_fail doctests", check_compile_fail_doctests(self.config, self.src))

    def test_a_deleted_doctest_is_caught(self) -> None:
        mutated = self.src.replace("```compile_fail", "```ignore", 1)
        self.assertNotEqual(mutated, self.src, "doctest mutation did not apply")
        self.assertCaught(check_compile_fail_doctests, mutated, "compile_fail")


class RestSurfaceTest(unittest.TestCase):
    """The REST guards read files from disk, so they mutate a temporary tree."""

    def setUp(self) -> None:
        self.config = load_config()

    def test_baseline_passes(self) -> None:
        self.assertIn("served_by=SRS-RESV-005", check_rest_surface(self.config))

    def _tree_with(
        self, tmp: Path, *, handler: str | None = None, routes: str | None = None
    ) -> Path:
        """A minimal mirror of the paths check_rest_surface reads."""
        for relative in (
            "python/atp_api/routes.py",
            "python/atp_orchestration/hot_swap_execution.py",
        ):
            target = tmp / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8")
        if handler is not None:
            (tmp / "python/atp_orchestration/hot_swap_execution.py").write_text(
                handler, encoding="utf-8"
            )
        if routes is not None:
            (tmp / "python/atp_api/routes.py").write_text(routes, encoding="utf-8")
        return tmp

    def test_a_timeout_that_no_longer_outlasts_the_demotion_is_caught(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = self._tree_with(Path(raw))
            handler_path = tmp / "python/atp_orchestration/hot_swap_execution.py"
            handler_path.write_text(
                handler_path.read_text(encoding="utf-8").replace(
                    "_DEFAULT_TIMEOUT_S = 90.0", "_DEFAULT_TIMEOUT_S = 30.0", 1
                ),
                encoding="utf-8",
            )
            with self.assertRaises(HotSwapPromotionCheckError) as ctx:
                check_rest_surface(self.config, tmp)
            self.assertIn("disagrees with the contract", str(ctx.exception))

    def test_a_dropped_served_by_declaration_is_caught(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = self._tree_with(Path(raw))
            routes_path = tmp / "python/atp_api/routes.py"
            routes_path.write_text(
                routes_path.read_text(encoding="utf-8").replace(
                    'served_by="SRS-RESV-005"', 'served_by=""', 1
                ),
                encoding="utf-8",
            )
            with self.assertRaises(HotSwapPromotionCheckError) as ctx:
                check_rest_surface(self.config, tmp)
            self.assertIn("served_by", str(ctx.exception))


class StaleDeferralCollectorTest(unittest.TestCase):
    def test_baseline_passes(self) -> None:
        self.assertIn("0 matches", check_no_stale_deferral())

    def test_a_reintroduced_stale_claim_is_caught(self) -> None:
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            relative = "python/atp_orchestration/hot_swap_triggers.py"
            target = tmp / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / relative, target)
            # Exactly the sentence this feature had to delete.
            target.write_text(
                target.read_text(encoding="utf-8")
                + "\n# nothing happened (swap execution is unbuilt)\n",
                encoding="utf-8",
            )
            with self.assertRaises(HotSwapPromotionCheckError) as ctx:
                check_no_stale_deferral(tmp)
            self.assertIn("swap execution is unbuilt", str(ctx.exception))

    def test_the_collector_scans_more_than_one_file(self) -> None:
        # A collector pointed at a single file would pass forever once that file
        # was fixed, which is exactly the failure mode it exists to prevent.
        from hot_swap_promotion_check import _PROSE_FILES, _STALE_CLAIMS

        self.assertGreaterEqual(len(_PROSE_FILES), 5)
        self.assertGreaterEqual(len(_STALE_CLAIMS), 3)


if __name__ == "__main__":
    unittest.main()
