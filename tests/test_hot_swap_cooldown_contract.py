"""Contract tests for SRS-RESV-006's anti-bypass guard (SyRS SYS-49e).

Mirrors ``tests/test_hot_swap_promotion_contract.py``: shells out to
``tools/hot_swap_cooldown_check.py``, then exercises the discovery collector
in-process against a MUTATED tree.

The mutations are the point, and this guard in particular earned them. Its first
version NAMED two methods — ``evaluate_automatic_triggers`` and
``request_manual_promotion`` — and asserted nothing about anything else. It passed
happily while ``execute_hot_swap`` demoted and promoted with no cool-down at all,
which is precisely what adversarial review round 2 found (``cooldown-execution-bypass``).
A checklist of known arms cannot catch the arm nobody added to it.

So the replacement DISCOVERS swap paths from source, and the cases below are what
make that discovery evidence rather than a grep that happens to match today:

  * an ungated swap path is caught (the r2 regression itself);
  * a swap path added LATER — one this contract has never heard of — is caught,
    which is the whole reason the check reads the source instead of a list;
  * drifted markers that match nothing FAIL rather than passing vacuously;
  * a known swap path vanishing from the discovery FAILS, so a broken scan cannot
    look like a clean tree;
  * the declared exemptions still let the real demotion-only paths through, so the
    guard's precision was not bought with false positives (adversarial-precheck:
    "parametrize BOTH directions").
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from hot_swap_cooldown_check import (  # noqa: E402
    HotSwapCooldownCheckError,
    check_every_swap_path_is_gated,
    check_the_window_is_committed_after_the_publish,
    check_trigger_arms_are_gated,
    load_config,
)

#: The real gate, relative to the repo root.
PROMOTION_MODULE = "crates/atp-orchestrator/src/hot_swap_promotion.rs"
ORCHESTRATOR_SRC = "crates/atp-orchestrator/src"


class HotSwapCooldownScriptTest(unittest.TestCase):
    def test_the_shipped_tree_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, str(TOOLS_ROOT / "hot_swap_cooldown_check.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-RESV-006 PASS", result.stdout)


class AntiBypassDiscoveryTest(unittest.TestCase):
    """Every case runs the collector against a COPY of the tree, mutated on disk.

    A copy, not the real tree: the collector walks ``rglob("*.rs")`` from a root, so
    handing it a scratch root is the honest way to test it — monkeypatching the
    function that holds the bug would test nothing (test-integrity rule 25).
    """

    def setUp(self) -> None:
        import tempfile

        self.config = load_config()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        shutil.copytree(ROOT / ORCHESTRATOR_SRC, self.root / ORCHESTRATOR_SRC)
        self.addCleanup(self._tmp.cleanup)

    def _module(self) -> Path:
        return self.root / PROMOTION_MODULE

    def _rewrite(self, old: str, new: str) -> None:
        path = self._module()
        source = path.read_text(encoding="utf-8")
        self.assertEqual(source.count(old), 1, f"mutation anchor is not unique: {old!r}")
        path.write_text(source.replace(old, new), encoding="utf-8")

    def test_the_unmutated_copy_passes(self) -> None:
        # The control. Without it, every case below could be passing because the
        # copy is broken rather than because the mutation was caught.
        evidence = check_every_swap_path_is_gated(self.config, self.root)
        self.assertIn("gated on `proven_clear`", evidence)

    def test_an_ungated_execution_entry_point_is_caught(self) -> None:
        # The round-2 regression itself: the gate deleted from `execute_hot_swap`.
        # Anchored on the expression as it is TODAY. Round 10 replaced
        # `cooldown.state` with a window the gate resolves itself, and this anchor
        # went stale — the mutation stopped applying and `_rewrite` said so, which is
        # why it asserts the count rather than silently replacing nothing.
        self._rewrite(
            "if !window.proven_clear() && !cooldown.acknowledgement.is_acknowledged() {",
            "if false {",
        )
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_every_swap_path_is_gated(self.config, self.root)
        self.assertIn("execute_hot_swap", str(caught.exception))
        self.assertIn("never consult", str(caught.exception))

    def test_a_swap_path_added_LATER_is_caught_without_touching_this_contract(self) -> None:
        # The reason the check reads the source instead of a list. Nothing in
        # `runtime_services.json` has ever heard of this function, and it is still
        # caught — which is exactly what the two-method version could not do.
        path = self._module()
        path.write_text(
            path.read_text(encoding="utf-8")
            + """
impl StrategyOrchestrator {
    /// A future second execution surface that forgot the cool-down.
    pub fn execute_hot_swap_v2(&self) {
        let _ = self.resolve_demotion();
    }
}
""",
            encoding="utf-8",
        )
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_every_swap_path_is_gated(self.config, self.root)
        self.assertIn("execute_hot_swap_v2", str(caught.exception))

    def test_a_gated_new_swap_path_is_allowed_through(self) -> None:
        # The other direction. A guard that flagged every new function regardless of
        # whether it consults the predicate would buy its precision with false
        # positives nobody tested for.
        path = self._module()
        path.write_text(
            path.read_text(encoding="utf-8")
            + """
impl StrategyOrchestrator {
    pub fn execute_hot_swap_v2(&self, cooldown: &CooldownState) {
        if !cooldown.proven_clear() {
            return;
        }
        let _ = self.resolve_demotion();
    }
}
""",
            encoding="utf-8",
        )
        evidence = check_every_swap_path_is_gated(self.config, self.root)
        self.assertIn("gated on `proven_clear`", evidence)

    def test_a_reintroduced_caller_supplied_state_field_is_caught(self) -> None:
        """Adversarial review r12 [high] — the guard against r10 was itself inert.

        The check rejects a `state:` field on `CooldownControl`, which is the forged
        proof round 10 removed. Its regex was written through a non-raw Python string,
        so `\\b` became a literal backspace (`\\x08`) and the pattern could never
        match — CI would have gone green while the API accepted a caller-supplied
        cool-down state again.

        A guard for a critical bypass with no test of its own is a grep that happens
        to match today. This is that test.
        """
        module = self.root / PROMOTION_MODULE
        source = module.read_text(encoding="utf-8")
        anchor = "pub struct CooldownControl<'a, W>"
        assert anchor in source
        brace = source.index("{", source.index(anchor))
        mutated = source[: brace + 1] + "\n    pub state: &'a CooldownState," + source[brace + 1 :]
        module.write_text(mutated, encoding="utf-8")

        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_the_window_is_committed_after_the_publish(self.config, self.root)
        self.assertIn("forgeable", str(caught.exception))

    def test_the_resolver_call_is_required_in_the_entry_point(self) -> None:
        # The other half of r10's guard: the gate must READ the window, not take one.
        module = self.root / PROMOTION_MODULE
        source = module.read_text(encoding="utf-8")
        mutated = source.replace(
            "let window = cooldown.store.resolve_window(observed_at_seconds);",
            "let window = CooldownState::NeverSwapped;",
        )
        self.assertNotEqual(mutated, source, "resolver mutation did not apply")
        module.write_text(mutated, encoding="utf-8")

        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_the_window_is_committed_after_the_publish(self.config, self.root)
        self.assertIn("resolve_window", str(caught.exception))

    def test_markers_that_match_nothing_fail_rather_than_pass_vacuously(self) -> None:
        # A guard that quietly stops looking is worse than no guard (CLAUDE.md r9).
        config = load_config()
        config["hot_swap_cooldown_contract"]["guard"]["swap_path_markers"] = [
            "a_marker_that_no_longer_exists("
        ]
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_every_swap_path_is_gated(config, self.root)
        self.assertIn("vacuously", str(caught.exception))

    def test_a_known_swap_path_vanishing_from_the_discovery_fails(self) -> None:
        # If the scan stops seeing `execute_hot_swap`, the DISCOVERY is broken — and
        # a broken discovery reports a clean tree, which is the failure mode that
        # looks most like working.
        config = load_config()
        config["hot_swap_cooldown_contract"]["guard"]["must_be_discovered"] = [
            "a_function_that_is_not_there"
        ]
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_every_swap_path_is_gated(config, self.root)
        self.assertIn("discovery is broken", str(caught.exception))

    def test_the_demotion_only_paths_stay_exempt(self) -> None:
        # The exemptions are a decision, not an oversight: an operator must always be
        # able to STOP trading a strategy, so the demotion half is deliberately not
        # gated. This pins that the declared set still covers them, so a future edit
        # that silently drops one turns the guard red instead of changing policy.
        guard = self.config["hot_swap_cooldown_contract"]["guard"]
        exempt = {entry["method"] for entry in guard["ungated_swap_paths"]}
        self.assertIn("resolve_demotion", exempt)
        self.assertIn("run_fixture_demotion", exempt)
        for entry in guard["ungated_swap_paths"]:
            self.assertTrue(
                entry["reason"].strip(),
                f"{entry['method']} is exempt with no stated reason",
            )


class TriggerArmsTest(unittest.TestCase):
    """The CLOSED half: the two trigger arms, checked by name."""

    def setUp(self) -> None:
        self.config = load_config()
        self.source = (ROOT / "crates/atp-orchestrator/src/lib.rs").read_text(encoding="utf-8")

    def test_the_shipped_arms_are_gated(self) -> None:
        evidence = check_trigger_arms_are_gated(self.config, self.source)
        self.assertIn("proven_clear", evidence)

    def test_an_ungated_automatic_arm_is_caught(self) -> None:
        # Anchored on the DECLARATION's span, not on a token the file repeats
        # (test-integrity rule 27): `proven_clear` appears in several bodies, so a
        # blanket replace would mutate the wrong subject and the guard would receive
        # an intact one — which reads exactly like "the guard works".
        import re

        match = re.search(r"\bpub fn evaluate_automatic_triggers\b[^{]*\{", self.source)
        assert match is not None
        start, depth, index = match.end(), 1, match.end()
        while depth:
            if self.source[index] == "{":
                depth += 1
            elif self.source[index] == "}":
                depth -= 1
            index += 1
        body = self.source[start : index - 1]
        mutated = (
            self.source[:start]
            + body.replace("proven_clear", "always_clear")
            + self.source[index - 1 :]
        )

        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_trigger_arms_are_gated(self.config, mutated)
        self.assertIn("evaluate_automatic_triggers", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
