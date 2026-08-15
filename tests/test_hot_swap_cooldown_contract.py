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
    check_store_durability,
    check_the_contract_names_nothing_that_moved,
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
            "if !window.proven_clear() && !cooldown.origin.waives_cooldown() {",
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

    def test_a_gate_that_never_opens_the_provisional_window_is_caught(self) -> None:
        """Adversarial review r13 [critical] — the fail-open on the OTHER side of r6.

        Round 6 moved the window write after the durable publish. Those are two
        separate file writes, so a crash between them leaves the candidate live with
        no window: the automatic triggers stay armed on a strategy that was just
        promoted, and nothing reports it. Phase one closes that; deleting phase one
        must turn the guard red, or the next edit reopens it silently.
        """
        self._rewrite(
            "if let Err(reason) = cooldown.store.begin_provisional_window(&provisional) {",
            "if let Err(reason) = Ok::<(), String>(()) {",
        )
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_the_window_is_committed_after_the_publish(self.config, self.root)
        self.assertIn("begin_provisional_window", str(caught.exception))
        self.assertIn("fail-open", str(caught.exception))

    def test_a_provisional_window_opened_too_LATE_is_caught(self) -> None:
        """Phase one must precede the demotion, not merely exist.

        A `begin_provisional_window` sitting after `resolve_demotion` is inside the
        very region it exists to cover — the liquidation can run for the whole
        SYS-49b timeout, and a crash anywhere in it lands back on r13's fail-open.
        The check therefore asserts the ORDER, and this moves the call to prove it:
        an assertion that only checks presence passes on the defect.
        """
        module = self._module()
        source = module.read_text(encoding="utf-8")
        opener = (
            "        if let Err(reason) = cooldown.store.begin_provisional_window(&provisional) {"
        )
        start = source.index(opener)
        end = source.index("\n        }\n", start) + len("\n        }\n")
        block = source[start:end]
        without = source[:start] + source[end:]
        # AFTER the demotion, which is the defect. Re-inserting it anywhere still
        # ahead of `resolve_demotion` would be a no-op reorder that proves nothing —
        # the first draft of this test did exactly that and passed on the defect.
        anchor = "        if outcome.is_err() {"
        moved = without.replace(anchor, block + anchor, 1)
        self.assertNotEqual(moved, without, "reorder mutation did not apply")
        self.assertGreater(
            moved.index("begin_provisional_window(&provisional)"),
            moved.index("let outcome = match self.resolve_demotion("),
            "the mutation must place phase one AFTER the demotion, or it tests nothing",
        )
        module.write_text(moved, encoding="utf-8")

        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_the_window_is_committed_after_the_publish(self.config, self.root)
        self.assertIn("BEFORE", str(caught.exception))
        self.assertIn("resolve_demotion", str(caught.exception))

    def test_a_gate_that_never_abandons_the_provisional_window_is_caught(self) -> None:
        # r6's direction, which r13 must not undo: a swap that FAILS leaves no
        # window, or a failed changeover suppresses the triggers for seven days.
        self._rewrite(
            "cooldown.store.abandon_provisional_window(&provisional);",
            "let _ = &provisional;",
        )
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_the_window_is_committed_after_the_publish(self.config, self.root)
        self.assertIn("abandon_provisional_window", str(caught.exception))

    def test_a_publish_failure_that_keeps_the_provisional_window_is_caught(self) -> None:
        """r6's guarantee, restated for r13 — and checked, not assumed to survive.

        Under one phase, "the publish failed" and "drop the token" were the same act
        and dropping opened nothing. Phase one writes BEFORE the demotion, so the same
        code now leaves a provisional window standing for a swap whose designation
        provably never moved. The CLI must give it back; deleting that call must be
        caught here rather than by the next reviewer.
        """
        cli = self.root / self.config["hot_swap_cooldown_contract"]["guard"]["promote_cli"]
        source = cli.read_text(encoding="utf-8")
        anchor = "                if let Ok(promoted) = outcome {\n                    promoted.abandon(&store);\n                }\n"
        self.assertEqual(source.count(anchor), 1, "abandon-on-publish-failure anchor drifted")
        cli.write_text(source.replace(anchor, ""), encoding="utf-8")

        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_the_window_is_committed_after_the_publish(self.config, self.root)
        self.assertIn("FailedBeforePublish", str(caught.exception))
        self.assertIn("owed to nobody", str(caught.exception))

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


class StoreWriterGuardTest(unittest.TestCase):
    """Every writer holds the O_EXCL guard — not just the one the check was born naming.

    This is the r12 lesson applied to the r13 diff. Before the two-phase protocol
    the store had ONE writer and ``check_store_durability`` asserted about it by
    name; r13 added three more read-modify-writes on the same file, and the check
    would have kept passing while any of them raced the resolver into reading a
    half-applied window. The declared set is now enumerated in the contract, and
    these cases prove the enumeration is enforced rather than decorative.
    """

    def setUp(self) -> None:
        self.config = load_config()
        self.source = (ROOT / "crates/atp-orchestrator/src/cooldown_store.rs").read_text(
            encoding="utf-8"
        )

    def test_the_shipped_store_passes(self) -> None:
        evidence = check_store_durability(self.config, self.source)
        self.assertIn("writers are guarded", evidence)

    def test_an_unguarded_writer_is_caught(self) -> None:
        # `abandon_provisional` is r13's clear-down: it reads the record, decides
        # whether the window is ours, and rewrites. Without the guard it interleaves
        # with a concurrent `record_completion` and one of the two writes is lost —
        # and the lost one may be the window that suppresses.
        import re as _re

        match = _re.search(r"\bpub fn abandon_provisional\b[^{]*\{", self.source)
        assert match is not None
        start, depth, index = match.end(), 1, match.end()
        while depth:
            if self.source[index] == "{":
                depth += 1
            elif self.source[index] == "}":
                depth -= 1
            index += 1
        body = self.source[start : index - 1]
        self.assertIn(
            "ExclusiveGuard",
            body,
            "anchor drifted: abandon_provisional no longer takes the guard",
        )
        mutated = (
            self.source[:start]
            + body.replace("ExclusiveGuard", "NoGuardAtAll")
            + self.source[index - 1 :]
        )

        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_store_durability(self.config, mutated)
        self.assertIn("abandon_provisional", str(caught.exception))
        self.assertIn("ExclusiveGuard", str(caught.exception))

    def test_a_writer_that_reads_outside_its_own_guard_is_caught(self) -> None:
        """Adversarial review r20 [high] — and the reason delegation credit is limited.

        `probe_writable` proved the store was writable by loading the period OUTSIDE
        the lock and handing it to `set_period`, which reacquires the lock and wrote
        the stale value back. An operator's concurrent `configure --set-days 30` landed
        in that gap and was silently reverted, so the window the gate then enforced was
        shorter than the one the operator had configured and was watching.

        The behavioural test catches it; this catches the SHAPE, because the shape is
        general: a read-modify-write must happen under one guard, so the function that
        reads is the function that must hold it. The first version of this check gave
        delegation credit unconditionally and passed on the defect.
        """
        import re as _re

        match = _re.search(r"\bfn probe_writable\b[^{]*\{", self.source)
        assert match is not None
        start, depth, index = match.end(), 1, match.end()
        while depth:
            if self.source[index] == "{":
                depth += 1
            elif self.source[index] == "}":
                depth -= 1
            index += 1
        mutated = (
            self.source[:start] + "\n    let period = match load(path)? {\n"
            "        Some(record) => record.period,\n"
            "        None => CooldownPeriodDays::default(),\n"
            "    };\n    set_period(path, period).map(|_| ())\n" + self.source[index - 1 :]
        )
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_store_durability(self.config, mutated)
        self.assertIn("probe_writable", str(caught.exception))
        self.assertIn("without holding", str(caught.exception))

    def test_every_public_store_writer_is_declared(self) -> None:
        """The enumeration cannot go stale silently.

        A fifth writer added later and left out of ``guarded_writers`` would be
        unchecked, which is precisely the shape of the r2 bypass: a checklist cannot
        catch the arm nobody added to it. So the list is cross-checked against the
        functions that actually call ``save(``.
        """
        import re as _re

        declared = set(
            self.config["hot_swap_cooldown_contract"]["cooldown_store"]["guarded_writers"]
        )
        actual = {
            name
            for name in _re.findall(r"\bpub fn (\w+)", self.source)
            if "save(" in _any_fn(self.source, name)
        }
        self.assertTrue(actual, "no store writers discovered; this test would pass vacuously")
        self.assertEqual(
            actual - declared,
            set(),
            "a store writer is not in the contract's guarded_writers and is therefore unchecked",
        )


def _any_fn(source: str, name: str) -> str:
    import re as _re

    match = _re.search(rf"\bfn\s+{_re.escape(name)}\b[^{{]*{{", source)
    if not match:
        return ""
    start, depth, index = match.end(), 1, match.end()
    while index < len(source) and depth:
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
        index += 1
    return source[start : index - 1]


if __name__ == "__main__":
    unittest.main()


class ContractDriftTest(unittest.TestCase):
    """The registry may not describe symbols that moved (adversarial review r14).

    The ``deferred`` list is prose, so nothing behavioural covers it — and it is the
    part a future reviewer reads as the statement of what is deliberately still open.
    Round 14 found it saying the SYS-49e fail-open "needs a two-phase window ...
    belongs to whoever next revises this entity" while this branch shipped one, and
    naming the mechanism as ``SwapCompletionSink::probe_writable``: a type and a
    function both renamed three rounds earlier.
    """

    def setUp(self) -> None:
        self.config = load_config()

    def test_the_shipped_contract_names_only_symbols_that_exist(self) -> None:
        evidence = check_the_contract_names_nothing_that_moved(self.config, ROOT)
        self.assertIn("still exist", evidence)

    def test_a_renamed_symbol_left_in_the_prose_is_caught(self) -> None:
        # The r14 instance itself, replayed: the old name put back into the deferred
        # entry that carried it.
        config = load_config()
        block = config["hot_swap_cooldown_contract"]
        block["deferred"] = [
            *block["deferred"],
            "Closing it needs SwapCompletionSink::probe_writable, which this build "
            "does not have any more.",
        ]
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_the_contract_names_nothing_that_moved(config, ROOT)
        self.assertIn("SwapCompletionSink::probe_writable", str(caught.exception))

    def test_a_renamed_symbol_in_a_VALUE_is_caught_too(self) -> None:
        # Not just the prose: the same drift in a declared value must fail, or the
        # guard would only cover the half that happened to break this time.
        config = load_config()
        config["hot_swap_cooldown_contract"]["guard"]["completion_probe_note"] = (
            "the probe is HotSwapCooldownPort::probe_that_never_existed"
        )
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_the_contract_names_nothing_that_moved(config, ROOT)
        self.assertIn("probe_that_never_existed", str(caught.exception))

    def test_the_scan_fails_rather_than_passing_on_an_empty_tree(self) -> None:
        # A guard that quietly stops looking is worse than no guard (CLAUDE.md r9).
        import tempfile

        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(HotSwapCooldownCheckError) as caught:
                check_the_contract_names_nothing_that_moved(self.config, Path(empty))
        self.assertIn("vacuously", str(caught.exception))


class SwapOriginGuardTest(AntiBypassDiscoveryTest):
    """The waiver belongs to the manual origin and nowhere else (review r17).

    Inherits the mutated-copy fixture: every case rewrites a scratch tree and runs the
    real collector against it, never a monkeypatched function (test-integrity r25).
    """

    def test_the_shipped_tree_passes(self) -> None:
        evidence = check_the_window_is_committed_after_the_publish(self.config, self.root)
        self.assertIn("provisional window", evidence)

    def test_a_bare_acknowledgement_back_on_the_control_is_caught(self) -> None:
        # The r17 defect itself: a waiver with no proof of who is waiving. With the
        # acknowledgement on the control rather than on the manual origin, an automatic
        # proposal converted into a demotion request executes inside an active window.
        module = self._module()
        source = module.read_text(encoding="utf-8")
        anchor = "pub struct CooldownControl<'a, W>"
        brace = source.index("{", source.index(anchor))
        mutated = (
            source[: brace + 1]
            + "\n    pub acknowledgement: ManualCooldownAcknowledgement,"
            + source[brace + 1 :]
        )
        module.write_text(mutated, encoding="utf-8")

        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_the_window_is_committed_after_the_publish(self.config, self.root)
        self.assertIn("bare `acknowledgement`", str(caught.exception))

    def test_an_automatic_variant_that_grows_a_payload_is_caught(self) -> None:
        # The structural property, not just the name: `Automatic` must have nothing a
        # caller could put a waiver in. A future `Automatic { acknowledged: bool }`
        # reopens the hole while every existing test still passes.
        self._rewrite("    Automatic,\n", "    Automatic { acknowledged: bool },\n")
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_the_window_is_committed_after_the_publish(self.config, self.root)
        self.assertIn("unit variant", str(caught.exception))

    def test_an_automatic_swap_granted_the_waiver_is_caught(self) -> None:
        # And the behaviour behind the shape: SYS-49e has no automatic exemption.
        self._rewrite(
            "            Self::Automatic => false,", "            Self::Automatic => true,"
        )
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_the_window_is_committed_after_the_publish(self.config, self.root)
        self.assertIn("ignored for the configured period", str(caught.exception))

    def test_a_gate_that_stops_consulting_the_waiver_is_caught(self) -> None:
        # One place where the two SYS-49e rules meet. A gate that inlined its own
        # version of the decision would let them drift apart silently.
        self._rewrite(
            "if !window.proven_clear() && !cooldown.origin.waives_cooldown() {",
            "if !window.proven_clear() {",
        )
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_the_window_is_committed_after_the_publish(self.config, self.root)
        self.assertIn("waives_cooldown", str(caught.exception))


class GoverningWindowGuardTest(unittest.TestCase):
    """Read and write must share one definition of "the window in force" (review r19).

    They had already drifted once: `resolve` took the later of the two slots while
    `record_completion_inner` guarded only `last_completion`, so a confirmation with an
    older instant cleared a newer provisional marker and shortened a live window. A
    comment saying "keep these in sync" is not a mechanism; this is.
    """

    def setUp(self) -> None:
        self.config = load_config()
        self.source = (ROOT / "crates/atp-orchestrator/src/cooldown_store.rs").read_text(
            encoding="utf-8"
        )

    def test_the_shipped_store_passes(self) -> None:
        self.assertIn("writers are guarded", check_store_durability(self.config, self.source))

    def test_a_writer_that_guards_only_one_slot_is_caught(self) -> None:
        # The r19 defect itself.
        mutated = self.source.replace(
            "    if let Some(stored) = governing(&existing) {\n"
            "        if stored.completed_at_seconds > completion.completed_at_seconds {\n"
            "            return Ok(CompletionOutcome::KeptNewer {\n"
            "                stored: stored.clone(),\n"
            "                offered: completion.clone(),\n"
            "            });\n"
            "        }\n"
            "    }\n\n"
            "    // Phase two also RETIRES this swap's provisional record",
            "    if let Some(stored) = &previous {\n"
            "        if stored.completed_at_seconds > completion.completed_at_seconds {\n"
            "            return Ok(CompletionOutcome::KeptNewer {\n"
            "                stored: stored.clone(),\n"
            "                offered: completion.clone(),\n"
            "            });\n"
            "        }\n"
            "    }\n\n"
            "    // Phase two also RETIRES this swap's provisional record",
        )
        self.assertNotEqual(mutated, self.source, "mutation anchor drifted")
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_store_durability(self.config, mutated)
        self.assertIn("record_completion_inner", str(caught.exception))
        self.assertIn("window is in force", str(caught.exception))

    def test_a_reader_that_stops_using_the_shared_selector_is_caught(self) -> None:
        # The other half. A reader that disagreed with the writers would report a
        # shorter window than the one being enforced — the operator surface and the
        # gate describing different cool-downs.
        mutated = self.source.replace(
            "            let start = governing(&record);",
            "            let start = record.as_ref().and_then(|r| r.last_completion.as_ref());",
        )
        self.assertNotEqual(mutated, self.source, "reader mutation did not apply")
        with self.assertRaises(HotSwapCooldownCheckError) as caught:
            check_store_durability(self.config, mutated)
        self.assertIn("resolve", str(caught.exception))
