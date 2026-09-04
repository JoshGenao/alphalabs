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
import subprocess
import sys
import unittest
from pathlib import Path

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

    Both numbers grew by 7 when SRS-MD-005 added the restart-window guards
    (5 -> 12 static). Keeping them EXACT rather than `>=` is deliberate — a
    lower bound would let a check be dropped without anything going red, which
    is the whole failure this assertion exists to catch.
    """

    def test_run_checks_emits_every_static_item_plus_the_cargo_smoke(self) -> None:
        evidence = run_checks()
        # 12 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(evidence), 13)

    def test_assert_connectivity_static_emits_twelve_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_connectivity_static(config, ROOT)
        self.assertEqual(len(evidence), 12)


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
        """The `&self` exemptions rest on the borrow checker. A `RefCell` around
        the map would let a `&self` method admit a subscription, so the argument
        for those exemptions would silently stop holding."""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(
                self.config, self.market_data_src + "\nuse std::cell::RefCell;\n"
            )
        self.assertIn("interior mutability", str(ctx.exception))

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


if __name__ == "__main__":
    # Kept at the END of the file on purpose. It previously sat mid-file, ahead
    # of the SRS-MD-005 guard classes, so `python3 tests/test_connectivity_contract.py`
    # exited 0 having executed none of them — a green covering nothing the
    # feature added. (CI runs pytest, which collects the whole module either
    # way, so nothing was red; that is exactly what made it easy to miss.)
    unittest.main()
