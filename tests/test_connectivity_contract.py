"""Contract tests for ERR-2 (SRS-SAFE-003 + SRS-MD-005 + SyRS SYS-45/46/NFR-R2).

Mirrors ``tests/test_error_handling_contract.py``: shells out to
``tools/connectivity_check.py``, then exercises each per-check function
in-process, including negative spot-checks that verify the contract
actually catches regressions (forbidden vendor fields, missing variants,
broker calls leaking into the blocked branch, missing reconnect /
event-record calls).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from connectivity_check import (  # noqa: E402
    ConnectivityCheckError,
    assert_connectivity_static,
    check_brokerage_connectivity_port,
    check_connectivity_event_sink_port,
    check_connectivity_event_struct,
    check_connectivity_guard_in_submit_live_order,
    check_connectivity_state_enum,
    check_market_data_admission_enum,
    check_market_data_admission_sites,
    check_reachability_seam_is_unpinned,
    check_restart_escalation_arm,
    check_restart_phase_enum,
    check_restart_window_defaults,
    check_restart_window_gate_implementors,
    check_restart_window_producer,
    execution_source,
    load_config,
    market_data_source,
    producer_source,
    reachability_source,
    run_checks,
    types_source,
)


class ConnectivityCheckScriptTest(unittest.TestCase):
    def test_err_2_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/connectivity_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ERR-2 PASS", result.stdout)
        for needle in (
            "ConnectivityState with 3 states",
            "Connected, Unreachable, ScheduledRestartWindow",
            "SRS-SAFE-003 / SRS-MD-005",
            "ConnectivityEvent with the 4 required fields",
            "state, strategy_id, symbol, scheduled_restart",
            "rejects 4 forbidden broker/vendor fields",
            "BrokerageConnectivity with 2 methods",
            "state, request_reconnect",
            "ConnectivityEventSink with 1 method",
            "ConnectivityState::Connected",
            "OrderErrorCategory::ConnectivityBlocked",
            "connectivity.request_reconnect",
            "zero broker side effect (ERR-2)",
            "err_2_connectivity_blocked",
            # SRS-MD-005 — the restart-window producer.
            "RestartPhase with 4 phases",
            "Normal, Suspending, Restarting, Elapsed",
            "MarketDataAdmission with 3 outcomes",
            "60s lead / 300s window",
            "RestartPhase::Elapsed onto ConnectivityState::Unreachable",
            "request_subscription, subscribe",
            "ScheduledRestartConnectivity implements BrokerageConnectivity + RestartWindowGate",
            "outside the digest-pinned transport module",
            "production type implements `RestartWindowGate`",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class ConnectivityStateEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_all_three_states_present(self) -> None:
        evidence = check_connectivity_state_enum(self.config, self.types_src)
        for variant in ("Connected", "Unreachable", "ScheduledRestartWindow"):
            self.assertIn(variant, evidence)

    def test_missing_unreachable_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("Unreachable,", "UnreachableX,", 1)
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_state_enum(self.config, mutated)
        self.assertIn("Unreachable", str(ctx.exception))

    def test_missing_scheduled_restart_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("ScheduledRestartWindow,", "", 1)
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_state_enum(self.config, mutated)
        self.assertIn("ScheduledRestartWindow", str(ctx.exception))


class ConnectivityEventStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_the_four_required_fields(self) -> None:
        evidence = check_connectivity_event_struct(self.config, self.types_src)
        for field in ("state", "strategy_id", "symbol", "scheduled_restart"):
            self.assertIn(field, evidence)

    def test_struct_rejects_leaked_broker_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct ConnectivityEvent {\n    pub state: ConnectivityState,",
            "pub struct ConnectivityEvent {\n    pub broker: String,\n    pub state: ConnectivityState,",
            1,
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_event_struct(self.config, mutated)
        self.assertIn("broker", str(ctx.exception))

    def test_struct_rejects_leaked_session_id_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct ConnectivityEvent {\n    pub state: ConnectivityState,",
            "pub struct ConnectivityEvent {\n    pub ib_session_id: String,\n    pub state: ConnectivityState,",
            1,
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_event_struct(self.config, mutated)
        self.assertIn("ib_session_id", str(ctx.exception))

    def test_missing_scheduled_restart_field_is_caught(self) -> None:
        mutated = self.types_src.replace("pub scheduled_restart: bool,", "", 1)
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_event_struct(self.config, mutated)
        self.assertIn("scheduled_restart", str(ctx.exception))


class BrokerageConnectivityPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_port_exposes_state_and_request_reconnect(self) -> None:
        evidence = check_brokerage_connectivity_port(self.config, self.exec_src)
        self.assertIn("state", evidence)
        self.assertIn("request_reconnect", evidence)

    def test_missing_request_reconnect_is_caught(self) -> None:
        mutated = self.exec_src.replace("fn request_reconnect", "fn dropped_reconnect_method")
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_brokerage_connectivity_port(self.config, mutated)
        self.assertIn("request_reconnect", str(ctx.exception))


class ConnectivityEventSinkPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_port_exposes_record(self) -> None:
        evidence = check_connectivity_event_sink_port(self.config, self.exec_src)
        self.assertIn("record", evidence)

    def test_missing_record_method_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "fn record(&self, event: ConnectivityEvent)",
            "fn dropped_record_method(&self, event: ConnectivityEvent)",
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_event_sink_port(self.config, mutated)
        self.assertIn("record", str(ctx.exception))


class ConnectivityGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_broker_call_is_gated_on_connected_state(self) -> None:
        evidence = check_connectivity_guard_in_submit_live_order(self.config, self.exec_src)
        self.assertIn("ConnectivityState::Connected", evidence)
        self.assertIn("OrderErrorCategory::ConnectivityBlocked", evidence)
        self.assertIn("connectivity.request_reconnect", evidence)
        self.assertIn("zero broker side effect (ERR-2)", evidence)

    def test_broker_call_inside_blocked_branch_is_caught(self) -> None:
        # Mutate the blocked branch to call broker.submit_order — the
        # regression the regex check exists to catch.
        mutated = self.exec_src.replace(
            "connectivity.request_reconnect();",
            "let _ = broker.submit_order(submission.clone()); connectivity.request_reconnect();",
            1,
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_guard_in_submit_live_order(self.config, mutated)
        self.assertIn("zero broker side effect", str(ctx.exception))

    def test_missing_reconnect_call_in_blocked_branch_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "connectivity.request_reconnect();", "/* reconnect removed */", 1
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_guard_in_submit_live_order(self.config, mutated)
        self.assertIn("request_reconnect", str(ctx.exception))

    def test_missing_event_record_call_in_blocked_branch_is_caught(self) -> None:
        # Strip the whole events.record(ConnectivityEvent { ... }); block so
        # the remaining source still parses.
        marker_open = "events.record(ConnectivityEvent {"
        start = self.exec_src.find(marker_open)
        self.assertGreaterEqual(start, 0, "could not locate events.record(...) in execution source")
        depth = 0
        index = start + len(marker_open) - 1  # position at the `{`
        # Walk to the matching closing brace of the struct literal.
        while index < len(self.exec_src):
            char = self.exec_src[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        # After the closing brace expect `);` to terminate the call.
        end = self.exec_src.find(";", index) + 1
        mutated = self.exec_src[:start] + "/* event removed */" + self.exec_src[end:]
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_guard_in_submit_live_order(self.config, mutated)
        self.assertIn("events.record", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    """The count is the pin: a check that silently stops running is invisible.

    Both numbers grew by 8 when SRS-MD-005 added the restart-window guards
    (5 -> 13 static). Keeping them EXACT rather than `>=` is deliberate — a
    lower bound would let a check be dropped without anything going red, which
    is the whole failure this assertion exists to catch, and it is why adding
    the gate-implementor enumeration turned this red rather than passing
    silently.
    """

    def test_run_checks_emits_every_static_item_plus_the_cargo_smoke(self) -> None:
        evidence = run_checks()
        # 13 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(evidence), 14)

    def test_assert_connectivity_static_emits_thirteen_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_connectivity_static(config, ROOT)
        self.assertEqual(len(evidence), 13)


# --------------------------------------------------------------------------- #
# SRS-MD-005 — the scheduled restart window (SyRS SYS-75)
# --------------------------------------------------------------------------- #
#
# Every guard below gets a mutation that must make it fail. A guard with no test
# of its own cannot tell you it is inert, and an inert guard over a safety bypass
# is worse than no guard: it reports a clean tree forever.


class RestartWindowTypesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_all_four_phases_present(self) -> None:
        evidence = check_restart_phase_enum(self.config, self.types_src)
        for variant in ("Normal", "Suspending", "Restarting", "Elapsed"):
            self.assertIn(variant, evidence)

    def test_a_dropped_phase_is_caught(self) -> None:
        mutated = self.types_src.replace("    Suspending,", "", 1)
        with self.assertRaises(ConnectivityCheckError):
            check_restart_phase_enum(self.config, mutated)

    def test_all_three_admission_outcomes_present(self) -> None:
        evidence = check_market_data_admission_enum(self.config, self.types_src)
        self.assertIn("ConnectivityLost", evidence)

    def test_collapsing_the_outage_outcome_is_caught(self) -> None:
        # Losing ConnectivityLost is how a genuine outage starts being reported
        # to the operator as planned maintenance.
        mutated = self.types_src.replace("    ConnectivityLost,", "", 1)
        with self.assertRaises(ConnectivityCheckError):
            check_market_data_admission_enum(self.config, mutated)

    def test_defaults_match_the_syrs_sys_75_values(self) -> None:
        evidence = check_restart_window_defaults(self.config, self.types_src)
        self.assertIn("60s lead / 300s window", evidence)

    def test_a_drifted_default_is_caught(self) -> None:
        # A documented default the code does not implement is a lie with a fuse.
        mutated = self.types_src.replace(
            "DEFAULT_RESTART_WINDOW_SECONDS: i64 = 300;",
            "DEFAULT_RESTART_WINDOW_SECONDS: i64 = 30;",
            1,
        )
        self.assertNotEqual(mutated, self.types_src, "the default constant anchor moved")
        with self.assertRaises(ConnectivityCheckError):
            check_restart_window_defaults(self.config, mutated)

    def test_a_public_window_field_is_caught(self) -> None:
        # A public field lets a caller build a window that skipped validation.
        mutated = self.types_src.replace(
            "pub struct RestartWindow {\n    suspend_from_ns: i64,",
            "pub struct RestartWindow {\n    pub suspend_from_ns: i64,",
            1,
        )
        self.assertNotEqual(mutated, self.types_src, "the RestartWindow field anchor moved")
        with self.assertRaises(ConnectivityCheckError):
            check_restart_window_defaults(self.config, mutated)


class RestartEscalationTest(unittest.TestCase):
    """The window must END.

    An `Elapsed` phase that kept returning the maintenance state would suppress
    a real outage indefinitely, and no other test in the tree would notice — the
    notification dispatcher would go on honouring a marker that never cleared.
    """

    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_escalation_is_declared(self) -> None:
        evidence = check_restart_escalation_arm(self.config, self.types_src)
        self.assertIn("RestartPhase::Elapsed", evidence)
        self.assertIn("ConnectivityState::Unreachable", evidence)

    def test_a_window_that_never_closes_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "RestartPhase::Normal | RestartPhase::Elapsed => {",
            "RestartPhase::Normal => {",
            1,
        )
        self.assertNotEqual(mutated, self.types_src, "the escalation arm anchor moved")
        with self.assertRaises(ConnectivityCheckError):
            check_restart_escalation_arm(self.config, mutated)

    def test_a_catch_all_arm_is_caught(self) -> None:
        # A catch-all means a phase added later inherits whichever answer
        # happened to be there rather than failing to compile.
        mutated = self.types_src.replace(
            "            RestartPhase::Suspending => ConnectivityState::ScheduledRestartWindow,",
            "            _ => ConnectivityState::ScheduledRestartWindow,",
            1,
        )
        self.assertNotEqual(mutated, self.types_src, "the Suspending arm anchor moved")
        with self.assertRaises(ConnectivityCheckError):
            check_restart_escalation_arm(self.config, mutated)


class MarketDataAdmissionSitesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.market_data_src = market_data_source(self.config)

    def _inject(self, snippet: str) -> str:
        """Splice a bypass into PRODUCTION code, before the test module.

        Appending it to the end of the file puts it inside — or after — the
        unshipped `#[cfg(test)] mod tests`, which the check correctly ignores.
        The test would then pass because the injection was invisible, not
        because the guard caught it: a control that proves nothing, shaped
        exactly like one that does.
        """
        head, marker, tail = self.market_data_src.partition("\n#[cfg(test)]\nmod tests {")
        # The owning module has no test module of its own; appending is then
        # already production code. Where a marker exists, splice before it.
        return head + snippet + marker + tail

    def test_both_admission_sites_are_gated(self) -> None:
        evidence = check_market_data_admission_sites(self.config, self.market_data_src)
        self.assertIn("request_subscription", evidence)
        self.assertIn("subscribe", evidence)

    def test_an_ungated_admission_site_is_caught(self) -> None:
        # Remove the guard from the MUTATING admission point. The function still
        # takes the port, so a checklist naming "these two functions accept a
        # window" would still pass — only reading the body catches it.
        # `subscribe`'s guard lives in the owning module; the manager's lives in
        # the crate root. This test mutates the one in the file it reads.
        marker = "        match window.admission() {"
        self.assertEqual(
            self.market_data_src.count(marker),
            1,
            "expected the registry's admission guard; the anchor has drifted",
        )
        mutated = self.market_data_src.replace(
            marker, "        match MarketDataAdmission::Admitted {", 1
        )
        with self.assertRaises(ConnectivityCheckError):
            check_market_data_admission_sites(self.config, mutated)

    def test_a_new_ungated_admission_point_is_caught(self) -> None:
        """The bypass the first version of this guard was blind to.

        Discovery used to key on "takes a RestartWindowGate", which is circular:
        a new path that SKIPS the port is exactly what must be caught, and
        skipping it made the path invisible. The adversarial reviewer proved it
        by injecting this function; the check reported "gates 2 admission
        site(s)" and passed. Discovery now keys on the EFFECT that makes a
        function an admission point, so omitting the port cannot hide it.
        """
        bypass = """
impl ConsolidatedSubscriptionRegistry {
    pub fn subscribe_bulk<S: SubscriptionChangeSink>(
        &mut self,
        requests: &[SubscriptionRequest],
        events: &S,
    ) -> Result<(), SubscriptionRegistryError> {
        for request in requests {
            let key = request.security_key()?;
            self.subscribers
                .insert(key.clone(), vec![request.strategy_id.clone()]);
            let _ = events;
        }
        Ok(())
    }
}
"""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, self._inject(bypass))
        self.assertIn("subscribe_bulk", str(ctx.exception))

    def test_a_declared_but_ungated_admission_point_is_caught(self) -> None:
        """Declaring it in the contract must not be enough — it must be gated."""
        config = json.loads(json.dumps(self.config))
        block = config["connectivity_contract"]["restart_window"]["admission_sites"]
        block["functions"] = [*block["functions"], "subscribe_bulk"]
        bypass = """
impl ConsolidatedSubscriptionRegistry {
    pub fn subscribe_bulk(&mut self, request: &SubscriptionRequest) {
        self.subscribers
            .insert(request.symbol.clone(), vec![request.strategy_id.clone()]);
    }
}
"""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(config, self._inject(bypass))
        self.assertIn("without calling", str(ctx.exception))

    def test_the_reviewers_entry_or_default_form_is_caught(self) -> None:
        """The bypass that walked past the SECOND version of this guard.

        That version discovered admission points by two literal effect forms;
        `subscribers.entry(k).or_default().push(..)` opens a consolidated line
        just as well and matched neither. A checklist cannot catch the arm
        nobody added to it, so discovery is now a CLOSED set over the type's
        `&mut self` surface — a new admission path has to be in it whatever
        expression it uses.
        """
        bypass = """
impl ConsolidatedSubscriptionRegistry {
    pub fn force_subscribe(&mut self, request: &SubscriptionRequest, key: SecurityKey) {
        self.subscribers
            .entry(key)
            .or_default()
            .push(request.strategy_id.clone());
    }
}
"""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, self._inject(bypass))
        self.assertIn("force_subscribe", str(ctx.exception))

    def test_a_mutator_is_required_to_be_gated_exactly_when_it_can_admit(self) -> None:
        """Where the closure draws its line, in both directions.

        The round-4 version of this guard required EVERY public `&mut self`
        method to be classified, including one that touched no subscriber map.
        The privacy-based closure is narrower and more honest: a method that
        cannot reach the consolidated set cannot admit, so demanding a
        restart-window guard on it would be noise. What matters is that the
        moment it DOES reach the set it is caught — which is the second half
        below.
        """
        harmless = """
impl ConsolidatedSubscriptionRegistry {
    pub fn retune(&mut self, limit: u32) {
        self.line_limit = limit;
    }
}
"""
        # It cannot admit, so it needs no guard and no exemption.
        check_market_data_admission_sites(self.config, self._inject(harmless))

        # One line later it touches the map — and is caught immediately.
        admitting = """
impl ConsolidatedSubscriptionRegistry {
    pub fn retune(&mut self, limit: u32, k: SecurityKey, s: StrategyId) {
        self.line_limit = limit;
        self.subscribers.insert(k, vec![s]);
    }
}
"""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, self._inject(admitting))
        self.assertIn("retune", str(ctx.exception))

    def test_an_exempt_name_cannot_be_reused_by_a_new_function(self) -> None:
        """Exempting by bare NAME is a hole.

        A trait impl reusing an exempt name inherits the exemption and is never
        asked to consult the window — the same trait-impl shape that had
        already defeated an earlier version of this guard. An exemption now has
        to resolve to exactly one function.
        """
        bypass = """
trait Sneaky { fn is_subscribed(&mut self, k: SecurityKey, s: StrategyId); }
impl Sneaky for ConsolidatedSubscriptionRegistry {
    fn is_subscribed(&mut self, k: SecurityKey, s: StrategyId) {
        self.subscribers.insert(k, vec![s]);
    }
}
"""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, self._inject(bypass))
        self.assertIn("is_subscribed", str(ctx.exception))

    def test_a_parenthesised_generic_bound_cannot_hide_a_second_declaration(self) -> None:
        """The hole the round-16 fix left one line away from the one it closed.

        `<[^{}();]*?>` excludes `(`, so a bound like `<F: Fn() -> bool>` ends the
        match early and the declaration is invisible. The scan then still counts
        exactly ONE `is_subscribed`, the exemption is inherited, and the new
        function reaches the consolidated registry without consulting the
        window - with every guard, the L7 suite and all three CLI proofs green.

        `Fn() -> _` is not an exotic shape here: the producer's own injected
        clock is one.
        """
        bypass = """
impl ConsolidatedSubscriptionRegistry {
    pub fn is_subscribed<F: Fn() -> bool>(&mut self, k: SecurityKey, s: StrategyId, f: F) {
        if f() {
            self.subscribers.insert(k, vec![s]);
        }
    }
}
"""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, self._inject(bypass))
        self.assertIn("is_subscribed", str(ctx.exception))

    def test_a_nested_generic_bound_cannot_hide_a_second_declaration(self) -> None:
        """One level of nesting was enough until `<T: Into<Vec<u8>>>` arrived.

        Every regex attempt at bounding a generic list failed on a shape it did
        not anticipate: a `->` closed it early, then a parenthesis, then two
        levels of nesting. The scan counts brackets now, so it has no depth
        limit at all.
        """
        bypass = """
impl ConsolidatedSubscriptionRegistry {
    pub fn is_subscribed<T: Into<Vec<u8>>>(&mut self, k: SecurityKey, s: StrategyId, v: T) {
        let _ = v;
        self.subscribers.insert(k, vec![s]);
    }
}
"""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, self._inject(bypass))
        self.assertIn("is_subscribed", str(ctx.exception))

    def test_a_semicolon_inside_a_generic_bound_cannot_hide_a_declaration(self) -> None:
        """The counter FAILED OPEN, which is worse than the regex it replaced.

        `<T: Into<[u8; 4]>>` contains a `;` - legal, inside an array type. The
        counter bailed on it and returned the start index, so the declaration
        was silently DROPPED, the count stayed at 1, and the exemption was
        inherited by a function the scan could not read. Unparseable is not
        absent (CLAUDE.md rule 3).
        """
        bypass = """
impl ConsolidatedSubscriptionRegistry {
    pub fn is_subscribed<T: Into<[u8; 4]>>(&mut self, k: SecurityKey, s: StrategyId, v: T) {
        let _ = v;
        self.subscribers.insert(k, vec![s]);
    }
}
"""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, self._inject(bypass))
        self.assertIn("is_subscribed", str(ctx.exception))

    def test_an_exempt_reader_that_becomes_a_mutator_is_caught(self) -> None:
        """The exemption says the function CANNOT admit. Changing its receiver
        makes that claim false, so the claim has to be re-checked."""
        mutated = self.market_data_src.replace(
            "pub fn is_subscribed(&self,", "pub fn is_subscribed(&mut self,", 1
        )
        self.assertNotEqual(mutated, self.market_data_src, "the receiver anchor moved")
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, mutated)
        self.assertIn("&mut self", str(ctx.exception))

    def test_the_one_write_exemption_may_remove_but_never_add(self) -> None:
        """`unsubscribe` is exempt because releasing a line cannot admit one.
        The moment it can add, the exemption is false."""
        marker = "pub fn unsubscribe"
        start = self.market_data_src.index(marker)
        brace = self.market_data_src.index("{", self.market_data_src.index(")", start))
        mutated = (
            self.market_data_src[: brace + 1]
            + "\n        self.subscribers.insert(key.clone(), Vec::new());"
            + self.market_data_src[brace + 1 :]
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, mutated)
        self.assertIn("unsubscribe", str(ctx.exception))

    def test_a_submodule_of_the_owning_module_is_refused(self) -> None:
        """Rust exposes a private item to the defining module AND its
        descendants, so a submodule would be an unscanned file able to write the
        map — the closure would be one level short."""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, self.market_data_src + "\nmod inner;\n")
        self.assertIn("submodule", str(ctx.exception))

    def test_a_deref_assignment_admission_point_is_caught(self) -> None:
        """The comment stripper used to delete real code.

        It dropped every line whose first non-space character was `*`, meaning
        to catch block-comment continuations — and took
        `*self.subscribers.entry(k).or_default() = vec![s];` with it, so an
        ungated admission point vanished from the scanned source. A stripper
        that removes code is worse than the false positive it was added for.
        """
        bypass = """
impl ConsolidatedSubscriptionRegistry {
    pub fn force(&mut self, k: SecurityKey, s: StrategyId) {
        *self.subscribers.entry(k).or_default() = vec![s];
    }
}
"""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, self._inject(bypass))
        self.assertIn("force", str(ctx.exception))

    def test_the_write_exemption_cannot_add_through_an_alias(self) -> None:
        """`unsubscribe` claims it can remove but never add.

        Checking that against `subscribers.insert(` is not enough: a local alias
        (`let map = &mut self.subscribers; map.insert(..)`) walks straight past
        any check anchored on the field name, so the body must contain no add
        expression at all.
        """
        marker = "pub fn unsubscribe"
        start = self.market_data_src.index(marker)
        brace = self.market_data_src.index("{", self.market_data_src.index(")", start))
        mutated = (
            self.market_data_src[: brace + 1]
            + "\n        let map = &mut self.subscribers;"
            + "\n        map.insert(key.clone(), Vec::new());"
            + self.market_data_src[brace + 1 :]
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, mutated)
        self.assertIn("unsubscribe", str(ctx.exception))

    def test_interior_mutability_in_the_owning_module_is_refused(self) -> None:
        """The `&self` exemptions rest on the borrow checker, so ANYTHING that
        lets `&self` mutate dissolves them.

        The first version of this check banned three shapes and the test
        exercised one of them, while asserting the whole class — so a `Cell`,
        `OnceCell` or atomic would have reintroduced interior mutability and let
        an exempt reader admit a subscription with no guard failure.
        """
        for snippet in (
            "use std::cell::RefCell;",
            "use std::cell::Cell;",
            "use std::cell::OnceCell;",
            "use std::sync::atomic::AtomicUsize;",
            "use std::sync::OnceLock;",
            "    fn sneak(&self) { unsafe { } }",
        ):
            with self.assertRaises(ConnectivityCheckError) as ctx:
                check_market_data_admission_sites(
                    self.config, self.market_data_src + "\n" + snippet + "\n"
                )
            self.assertIn("interior mutability", str(ctx.exception), snippet)

        # Non-vacuity: the real module passes, so the refusals above are about
        # the snippets rather than about a check that refuses everything.
        check_market_data_admission_sites(self.config, self.market_data_src)

    def test_any_widened_visibility_on_the_field_is_caught(self) -> None:
        """The closure IS Rust's privacy, so any widening breaks it.

        `pub(crate)` re-opens the field to every sibling module — including
        `live_feed`, the exact module the registry was moved into its own file
        to escape — and the earlier check's `\\bpub\\s+` matched none of the
        parenthesised forms.
        """
        for visibility in ("pub", "pub(crate)", "pub(super)", "pub(in crate::live_feed)"):
            mutated = self.market_data_src.replace(
                "    subscribers: BTreeMap", f"    {visibility} subscribers: BTreeMap", 1
            )
            self.assertNotEqual(mutated, self.market_data_src, "the field anchor moved")
            with self.assertRaises(ConnectivityCheckError) as ctx:
                check_market_data_admission_sites(self.config, mutated)
            self.assertIn(visibility, str(ctx.exception))

    def test_the_crate_root_guard_cannot_be_satisfied_by_a_comment(self) -> None:
        """The manager's half of the guard read its source WITHOUT stripping
        comments, so deleting the real `match window.admission()` and leaving a
        comment that merely mentions it passed — a guard a docstring could
        satisfy. It also had no mutation test, because the module move left this
        class's fixture pointing only at the registry."""
        import connectivity_check as module

        lib_src = module.market_data_lib_source(self.config)
        start = lib_src.index("        match window.admission() {")
        end = lib_src.index("        match counter.try_acquire(&request) {")
        gutted = lib_src[:start] + "        // match window.admission() { removed\n" + lib_src[end:]

        original = module.market_data_lib_source
        module.market_data_lib_source = lambda config, root=None: gutted
        try:
            with self.assertRaises(ConnectivityCheckError) as ctx:
                check_market_data_admission_sites(self.config, self.market_data_src)
            self.assertIn("request_subscription", str(ctx.exception))
        finally:
            module.market_data_lib_source = original

        # Non-vacuity: with the real source restored the check passes again, so
        # the failure above was the mutation and not the monkeypatch.
        check_market_data_admission_sites(self.config, self.market_data_src)

    def test_a_stale_exemption_is_caught(self) -> None:
        """An exemption for a function that no longer exists is a hole nobody
        is watching."""
        config = json.loads(json.dumps(self.config))
        block = config["connectivity_contract"]["restart_window"]["admission_sites"]
        block["exempt"] = [*block["exempt"], "long_gone"]
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(config, self.market_data_src)
        self.assertIn("long_gone", str(ctx.exception))

    def test_exempting_a_real_admission_site_is_visible_in_the_evidence(self) -> None:
        """An exemption must not be silent: the evidence names how many there
        are, so removing a gate by exempting it shows up in the record."""
        evidence = check_market_data_admission_sites(self.config, self.market_data_src)
        self.assertIn("classified as unable to admit", evidence)
        self.assertIn("unsubscribe", evidence)

    def test_a_reformat_cannot_hide_an_admission_point(self) -> None:
        """rustfmt wraps `self.subscribers\n.insert(` across lines. A raw
        substring match would then miss the call that DEFINES an admission
        point, and a guard a reformat can disarm is not a guard."""
        wrapped = """
impl ConsolidatedSubscriptionRegistry {
    pub fn subscribe_wrapped(&mut self, request: &SubscriptionRequest) {
        self
            .subscribers
            .insert(
                request.symbol.clone(),
                vec![request.strategy_id.clone()],
            );
    }
}
"""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, self._inject(wrapped))
        self.assertIn("subscribe_wrapped", str(ctx.exception))

    def test_a_scan_that_matches_nothing_fails_rather_than_reporting_clean(self) -> None:
        # A broken discovery reports a clean tree, which is the failure mode
        # that looks most like the guard working.
        mutated = self.market_data_src.replace("    subscribers: BTreeMap", "    renamed: BTreeMap")
        self.assertNotEqual(mutated, self.market_data_src, "the private-field anchor moved")
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, mutated)
        self.assertIn("no longer a private field", str(ctx.exception))

    def test_a_public_subscriber_map_is_caught(self) -> None:
        """The closure IS Rust's privacy. If the field goes public, any crate
        can admit without passing through a gated function and no source scan
        can close that — so the check must refuse rather than keep reporting."""
        mutated = self.market_data_src.replace(
            "    subscribers: BTreeMap", "    pub subscribers: BTreeMap"
        )
        self.assertNotEqual(mutated, self.market_data_src, "the private-field anchor moved")
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, mutated)
        self.assertIn("not private", str(ctx.exception))


class RestartWindowProducerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.producer_src = producer_source(self.config)
        self.reachability_src = reachability_source(self.config)

    def test_one_producer_serves_both_gates(self) -> None:
        evidence = check_restart_window_producer(self.config, self.producer_src)
        self.assertIn("BrokerageConnectivity", evidence)
        self.assertIn("RestartWindowGate", evidence)

    def test_dropping_the_market_data_gate_is_caught(self) -> None:
        # Two separately-configured gates over one requirement is a defect
        # waiting for a deployment where only one of them was updated.
        mutated = self.producer_src.replace(
            "impl<P, C> atp_market_data::RestartWindowGate for ScheduledRestartConnectivity<P, C>",
            "impl<P, C> SomethingElse for ScheduledRestartConnectivity<P, C>",
            1,
        )
        self.assertNotEqual(mutated, self.producer_src, "the producer impl anchor moved")
        with self.assertRaises(ConnectivityCheckError):
            check_restart_window_producer(self.config, mutated)

    def test_the_probe_seam_is_bounded_and_unpinned(self) -> None:
        evidence = check_reachability_seam_is_unpinned(self.config, self.reachability_src)
        self.assertIn("never resolves a hostname", evidence)

    def test_a_hostname_resolving_probe_is_caught(self) -> None:
        # getaddrinfo blocks OUTSIDE connect_timeout's deadline, so a hostname
        # turns the bound into a suggestion.
        mutated = self.reachability_src.replace(
            "TcpStream::connect_timeout(&self.endpoint, self.timeout)",
            "self.endpoint.to_socket_addrs().and_then(TcpStream::connect)",
            1,
        )
        self.assertNotEqual(mutated, self.reachability_src, "the connect anchor moved")
        with self.assertRaises(ConnectivityCheckError):
            check_reachability_seam_is_unpinned(self.config, mutated)

    def test_an_unbounded_connect_is_caught(self) -> None:
        mutated = self.reachability_src.replace(
            "TcpStream::connect_timeout(&self.endpoint, self.timeout)",
            "TcpStream::connect(self.endpoint)",
            1,
        )
        self.assertNotEqual(mutated, self.reachability_src, "the connect anchor moved")
        with self.assertRaises(ConnectivityCheckError):
            check_reachability_seam_is_unpinned(self.config, mutated)


class GateImplementorScanTest(unittest.TestCase):
    """The scan that makes the port unforgeable, tested against real bypasses.

    This enumeration is the ONLY thing standing between the tree and a
    production `impl RestartWindowGate` that always returns `Admitted` — which
    would bypass the SyRS SYS-75(a) suspension with every other guard, the L7
    suite and all three CLI proofs still green. A guard in that position gets
    tested by trying to walk past it, not by being read.

    Both bypasses below were live holes found by adversarial review, not
    hypotheticals.
    """

    PRODUCER = "crates/atp-orchestrator/src/restart_window_connectivity.rs"

    def setUp(self) -> None:
        self.config = load_config()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # A minimal tree holding the REAL producer, so the only difference
        # between a clean run and a caught run is the injected bypass.
        dest = self.tmp / self.PRODUCER
        dest.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / self.PRODUCER, dest)

    def _inject(self, crate: str, body: str) -> None:
        path = self.tmp / "crates" / crate / "src" / "sneaky.rs"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_the_clean_tree_passes(self) -> None:
        """Non-vacuity: without this, both tests below pass on a broken scan."""
        evidence = check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("ScheduledRestartConnectivity", evidence)

    def test_an_arrow_in_the_generic_bounds_does_not_hide_an_implementor(self) -> None:
        """The `[^>]*` hole: that class terminates on the `>` of `->`.

        The producer's own clock is an `Fn() -> i64`, so this is the shape an
        implementor in this codebase naturally takes — the guard was blind to
        precisely the idiom the feature uses.
        """
        self._inject(
            "atp-orchestrator",
            """
pub struct AlwaysOpen<C> {
    clock: C,
}

impl<C: Fn() -> i64> atp_market_data::RestartWindowGate for AlwaysOpen<C> {
    fn admission(&self) -> MarketDataAdmission {
        MarketDataAdmission::Admitted
    }
}
""",
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("AlwaysOpen", str(ctx.exception))

    def test_an_implementor_in_any_crate_is_caught(self) -> None:
        """The hard-coded-crate-list hole.

        The scan listed the four crates that had the trait in view when it was
        written. Any crate that later gains an atp-market-data dependency could
        implement the gate unscanned, while the contract claimed the check
        "walks the crate sources".
        """
        self._inject(
            "atp-simulation",
            """
pub struct SimGate;

impl RestartWindowGate for SimGate {
    fn admission(&self) -> MarketDataAdmission {
        MarketDataAdmission::Admitted
    }
}
""",
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("SimGate", str(ctx.exception))

    def test_the_impl_target_is_a_type_not_an_identifier(self) -> None:
        """`for &X`, `for &'a X` and `for (X, u8)` all escaped a `(\\w+)` capture.

        Rust lets you implement a trait for a reference or a tuple, so requiring
        a bare identifier after `for` left the "closed set" open to exactly the
        always-admitting production gate the enumeration exists to forbid. Each
        shape below was verified against the old compiled pattern as a real
        bypass, not a hypothetical.
        """
        shapes = {
            "reference": "impl atp_market_data::RestartWindowGate for &AlwaysOpen {",
            "lifetime": "impl<'a> atp_market_data::RestartWindowGate for &'a AlwaysOpen {",
            "tuple": "impl atp_market_data::RestartWindowGate for (AlwaysOpen, u8) {",
            "where_clause": (
                "impl<C> atp_market_data::RestartWindowGate for AlwaysOpen<C>\n"
                "where\n    C: Fn() -> i64,\n{"
            ),
        }
        for label, header in shapes.items():
            with self.subTest(shape=label):
                self._inject(
                    "atp-orchestrator",
                    header
                    + """
    fn admission(&self) -> MarketDataAdmission {
        MarketDataAdmission::Admitted
    }
}
""",
                )
                with self.assertRaises(ConnectivityCheckError) as ctx:
                    check_restart_window_gate_implementors(self.config, "", root=self.tmp)
                self.assertIn("AlwaysOpen", str(ctx.exception))

    def test_a_type_parameter_is_not_mistaken_for_an_implementor(self) -> None:
        """The false-positive direction of the fix.

        Stripping generics is what stops `ScheduledRestartConnectivity<P, C>`
        reporting `P` and `C` as two undeclared production implementors, which
        would make the guard cry wolf on the declared producer itself.
        """
        evidence = check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("exactly 1 production type", evidence)

    def test_the_dispatcher_gives_root_to_every_check_that_takes_one(self) -> None:
        """The `root` parameter must be PASSED, not merely declared.

        Round 14 added `root` to this check and recorded the finding as fixed;
        `assert_connectivity_static` forwarded it only to `_sources`, so this
        one check kept its `ROOT` default and scanned the REAL repository while
        its twelve siblings scanned the caller's tree.

        The root under test is a DISTINCT directory, not `ROOT`. The first
        version of this test spied with `root=cc.ROOT` as the default and
        asserted the spy saw `ROOT` - which it did either way, so the test
        passed with the dispatcher's fix reverted. A guard that cannot fail is
        not a guard, and this one proved it on its own first mutation run.
        """
        import inspect

        import connectivity_check as cc

        takes_root = [
            name
            for name, check, _ in cc._STATIC_CHECKS
            if "root" in inspect.signature(check).parameters
        ]
        self.assertIn("restart_window_gate_implementors", takes_root)

        sentinel = self.tmp
        self.assertNotEqual(sentinel, ROOT, "the test root must differ from the real one")
        seen: dict[str, object] = {}

        def spy(config, source, root=cc.ROOT):
            seen["root"] = root
            return "spied"

        table = tuple(
            (n, spy if n == "restart_window_gate_implementors" else c, s)
            for n, c, s in cc._STATIC_CHECKS
        )
        # `_sources` reads real files, so it keeps the real tree; only the root
        # handed to the checks is the sentinel.
        real_sources = cc._sources
        with (
            mock.patch.object(cc, "_STATIC_CHECKS", table),
            mock.patch.object(cc, "_sources", lambda config, root=ROOT: real_sources(config, ROOT)),
        ):
            cc.assert_connectivity_static(self.config, sentinel)
        self.assertEqual(
            seen.get("root"),
            sentinel,
            "the dispatcher dropped `root`, so this check scans the real repo "
            "while its siblings scan the caller's tree",
        )

    def test_a_return_arrow_in_the_impl_target_does_not_cry_wolf(self) -> None:
        """The FALSE-POSITIVE direction, and the fourth arrow defect here.

        `_strip_generic_args` closed its bracket depth on the `>` of a `->`, so
        in `impl Gate for Wrapper<fn() -> EpochNanos>` the depth fell to zero at
        the arrow, ` EpochNanos` was emitted at depth 0, and the uppercase sweep
        reported it as an undeclared production implementor. The guard would
        `fail()` on a legal shape - which is how a guard gets disabled.
        """
        self._inject(
            "atp-orchestrator",
            """
pub struct Wrapper<T>(T);

impl atp_market_data::RestartWindowGate for Wrapper<fn() -> EpochNanos> {
    fn admission(&self) -> MarketDataAdmission {
        MarketDataAdmission::Admitted
    }
}
""",
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        msg = str(ctx.exception)
        self.assertIn("Wrapper", msg)
        self.assertNotIn(
            "EpochNanos", msg, "a return type was reported as a production implementor"
        )

    def test_a_renamed_trait_import_does_not_hide_an_implementor(self) -> None:
        """`use ... as Gate;` is a two-word rename that walked past the scan.

        The check kept printing "this enumeration is what makes it unforgeable"
        while producing no match at all for the aliased impl.
        """
        for label, header in {
            "simple use-as": (
                "use atp_market_data::RestartWindowGate as Gate;\nimpl Gate for AlwaysOpen {"
            ),
            "braced use-as": (
                "use atp_market_data::{RestartWindowGate as G2, MarketDataAdmission};\n"
                "impl G2 for AlwaysOpen {"
            ),
        }.items():
            with self.subTest(shape=label):
                self._inject(
                    "atp-orchestrator",
                    header
                    + """
    fn admission(&self) -> MarketDataAdmission {
        MarketDataAdmission::Admitted
    }
}
""",
                )
                with self.assertRaises(ConnectivityCheckError) as ctx:
                    check_restart_window_gate_implementors(self.config, "", root=self.tmp)
                self.assertIn("AlwaysOpen", str(ctx.exception))

    def test_an_impl_target_the_scan_cannot_name_fails_rather_than_collecting_nothing(self):
        """Unnameable is not absent (CLAUDE.md rule 3).

        Names were extracted with `\\b([A-Z]\\w*)`, so an impl target carrying no
        uppercase identifier collected NOTHING and the "closed set" stayed open
        while the check reported a clean tree - the worst possible failure mode
        for an enumeration whose whole claim is that it is exhaustive.
        """
        for label, target in {
            "primitive": "i64",
            "lowercase alias": "gate_alias",
            "array": "[u8; 4]",
        }.items():
            with self.subTest(target=label):
                self._inject(
                    "atp-orchestrator",
                    f"""
impl atp_market_data::RestartWindowGate for {target} {{
    fn admission(&self) -> MarketDataAdmission {{
        MarketDataAdmission::Admitted
    }}
}}
""",
                )
                with self.assertRaises(ConnectivityCheckError) as ctx:
                    check_restart_window_gate_implementors(self.config, "", root=self.tmp)
                msg = str(ctx.exception)
                # Either refusal is correct: the scan parsed the header and
                # could not NAME the target, or it could not parse the header at
                # all and the completeness backstop caught the shortfall. Both
                # end in a refusal rather than a clean report, which is the
                # property under test.
                self.assertTrue(
                    "cannot name" in msg or "could parse only" in msg,
                    msg,
                )

    def test_the_backstop_does_not_share_the_blind_spot_it_backs_up(self) -> None:
        """A backstop bounded like the pattern it backs up is not a backstop.

        The completeness count was written with `[^;]`, the same boundary the
        strict pattern used, so any shape a `;` defeated defeated BOTH and the
        scan reported `expected == matched == 0`: a clean, closed set with an
        always-admitting implementor sitting in it.
        """
        self._inject(
            "atp-orchestrator",
            """
pub struct AlwaysOpen;

impl<T: Into<[u8; 4]>> atp_market_data::RestartWindowGate for AlwaysOpen {
    fn admission(&self) -> MarketDataAdmission {
        MarketDataAdmission::Admitted
    }
}
""",
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        msg = str(ctx.exception)
        self.assertTrue(
            "AlwaysOpen" in msg or "could parse only" in msg,
            msg,
        )

    def test_a_two_hop_rename_does_not_hide_an_implementor(self) -> None:
        """Aliases were collected per-FILE, so a rename across modules escaped.

        `pub use RestartWindowGate as Gate;` in one module, then
        `use crate::gates::Gate; impl Gate for AlwaysOpen` in another, produced
        no strict match AND no loose match - a silent clean report, while the
        playbook recorded the rename class as closed.
        """
        (self.tmp / "crates" / "atp-orchestrator" / "src").mkdir(parents=True, exist_ok=True)
        (self.tmp / "crates" / "atp-orchestrator" / "src" / "gates.rs").write_text(
            "pub use atp_market_data::RestartWindowGate as Gate;\n", encoding="utf-8"
        )
        self._inject(
            "atp-orchestrator",
            """
use crate::gates::Gate;

pub struct AlwaysOpen;

impl Gate for AlwaysOpen {
    fn admission(&self) -> MarketDataAdmission {
        MarketDataAdmission::Admitted
    }
}
""",
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("AlwaysOpen", str(ctx.exception))

    def test_a_scan_that_finds_nothing_fails_rather_than_reporting_clean(self) -> None:
        """An empty tree is the failure mode a scan-based guard dies of."""
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)
        (empty / "crates").mkdir()
        with self.assertRaises(ConnectivityCheckError):
            check_restart_window_gate_implementors(self.config, "", root=empty)


if __name__ == "__main__":
    # Kept at the END of the file on purpose. It previously sat mid-file, ahead
    # of the SRS-MD-005 guard classes, so `python3 tests/test_connectivity_contract.py`
    # exited 0 having executed none of them — a green covering nothing the
    # feature added. (CI runs pytest, which collects the whole module either
    # way, so nothing was red; that is exactly what made it easy to miss.)
    unittest.main()
