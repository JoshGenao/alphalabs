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
    _code_only,
    _mask_non_code,
    _without_test_module,
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

    def test_every_function_that_reaches_the_map_is_read_not_just_the_first(self) -> None:
        """The identity class (r30) in this check: names are not functions.

        A second `fn subscribe` - a trait-impl method sharing the gated site's
        name - was never read: the check looked up "the body of `subscribe`" and
        got the first one, which is gated. Every function body is now checked on
        its own. And a function the span finder could not see at all reached the
        map unclassified: a `;` in an array parameter or a `{` in a const-generic
        argument ended its signature early, and a `"}"` in a string ended its body
        early, leaving the write outside the span it was searched in.
        """
        write = "        self.subscribers.insert(key, vec![id]);\n"
        shapes = {
            "a second fn with a gated site's name": (
                "pub trait Sneak { fn subscribe(&mut self, key: SecurityKey, id: StrategyId); }\n"
                "impl Sneak for ConsolidatedSubscriptionRegistry {\n"
                "    fn subscribe(&mut self, key: SecurityKey, id: StrategyId) {\n"
                f"{write}    }}\n}}\n"
            ),
            "a `;` in an array parameter": (
                "impl ConsolidatedSubscriptionRegistry {\n"
                "    pub fn sneak(&mut self, keys: [SecurityKey; 1], id: StrategyId) {\n"
                "        let [key] = keys;\n"
                f"{write}    }}\n}}\n"
            ),
            "a `{` in a const-generic argument": (
                "pub trait HasWidth<const N: usize> {}\n"
                "impl ConsolidatedSubscriptionRegistry {\n"
                "    pub fn sneak<T: HasWidth<{ 1 + 1 }>>(&mut self, key: SecurityKey, id: StrategyId) {\n"
                f"{write}    }}\n}}\n"
            ),
            "a `}` in a string before the write": (
                "impl ConsolidatedSubscriptionRegistry {\n"
                "    pub fn sneak(&mut self, key: SecurityKey, id: StrategyId) {\n"
                '        let _ = "}";\n'
                f"{write}    }}\n}}\n"
            ),
        }
        for label, bypass in shapes.items():
            with self.subTest(shape=label):
                with self.assertRaises(ConnectivityCheckError):
                    check_market_data_admission_sites(self.config, self._inject(bypass))

    def test_the_map_named_outside_any_function_is_refused(self) -> None:
        """A macro body is not a function, so a write inside one was in no span.

        `macro_rules! sneak { .. $r.subscribers.insert(..) .. }` invoked from
        `fn add` left `add` never naming the field, so it was never classified.
        Outside its declaration, the field may now appear only inside a function
        body of its own module.
        """
        bypass = (
            "macro_rules! sneak {\n"
            "    ($r:expr, $k:expr, $id:expr) => { $r.subscribers.insert($k, vec![$id]) };\n"
            "}\n"
            "impl ConsolidatedSubscriptionRegistry {\n"
            "    pub fn add(&mut self, key: SecurityKey, id: StrategyId) { sneak!(self, key, id); }\n"
            "}\n"
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, self._inject(bypass))
        self.assertIn("outside", str(ctx.exception))

    def test_a_submodule_with_any_visibility_is_refused(self) -> None:
        for decl in ("pub(crate) mod inner;", "pub(super) mod inner;", "pub(in crate) mod inner;"):
            with self.subTest(decl=decl):
                with self.assertRaises(ConnectivityCheckError) as ctx:
                    check_market_data_admission_sites(
                        self.config, self.market_data_src + f"\n{decl}\n"
                    )
                self.assertIn("submodule", str(ctx.exception))

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


class TestModuleStripperTest(unittest.TestCase):
    """Every scan in this file reads `_without_test_module`'s output first.

    It counted braces without knowing where a brace can HIDE, so a
    `#[cfg(test)] mod tests { let s = "{"; }` never closed and the stripper ate
    every production line after it. Each scan built on that then read an empty
    tail and reported a clean, closed set - the worst failure mode for an
    enumeration whose whole claim is exhaustiveness.

    The real tree uses all six of these; the raw-string case alone appears 46
    times in one adapter.
    """

    KEEP = "pub fn keep() { self.subscribers.insert(k, v); }"

    def _tree(self, body: str) -> str:
        return f"pub fn a() {{}}\n#[cfg(test)]\nmod tests {{\n    {body}\n}}\n{self.KEEP}\n"

    def test_a_brace_that_is_not_code_does_not_extend_the_test_module(self) -> None:
        for label, body in {
            "string literal": 'let s = "{";',
            "raw string": 'let s = r#"{"id":1}"#;',
            "line comment": "// a stray { here\n",
            "block comment": "/* a stray { here */",
            "lifetime": "fn g<'a>(x: &'a str) {}",
            "char literal": "let c = '}';",
        }.items():
            with self.subTest(shape=label):
                out = _without_test_module(self._tree(body))
                self.assertIn("keep", out, "production code after the module was swallowed")
                self.assertNotIn("mod tests", out, "the test module was not stripped")

    def test_an_unterminated_test_module_refuses_rather_than_truncating(self) -> None:
        """Unparseable is not empty. A truncated read must be red, not clean."""
        with self.assertRaises(ConnectivityCheckError) as ctx:
            _without_test_module('pub fn a() {}\n#[cfg(test)]\nmod tests {\n    let s = ";\n')
        self.assertIn("unterminated", str(ctx.exception))


class RustLexerTest(unittest.TestCase):
    """The one lexer every scan reads through, held to Rust's actual rules.

    Adversarial review r28 [high]: block comments NEST in Rust, and the lexer
    closed one at its first `*/`. In `/* outer /* inner */ " */` the remaining
    `"` was then read as a string opener, which blanked the always-admitting
    implementor after it, and all 13 static checks passed. Every form below is a
    way to make the lexer disagree with rustc about where code is; each one is
    pinned here, and a span the lexer cannot close is refused, never guessed.
    """

    def test_a_nested_block_comment_closes_where_rustc_closes_it(self) -> None:
        src = '/* outer /* inner */ " */ impl Gate for Evil {}'
        masked = _mask_non_code(src)
        self.assertEqual(len(masked), len(src))
        self.assertIn("impl Gate for Evil {}", masked)
        self.assertNotIn('"', masked)

    def test_a_comment_is_not_lexed_for_strings_or_chars(self) -> None:
        # rustc does not tokenize inside a comment, so neither may this.
        for label, src in {
            "a quote": '/* " */ impl Gate for Evil {}',
            "a raw-string opener": '/* r#" */ impl Gate for Evil {}',
            "a char opener": "/* ' */ impl Gate for Evil {}",
        }.items():
            with self.subTest(shape=label):
                self.assertIn("impl Gate for Evil {}", _mask_non_code(src))

    def test_an_escaped_quote_char_literal_is_one_token(self) -> None:
        # `'\''` closed on the ESCAPED quote, leaving a stray `'` in the code.
        for literal in ("'\\''", "'\\\\'", "'\\u{7b}'", "'\\x7b'", "'\"'", "'{'"):
            with self.subTest(literal=literal):
                src = f"let c = {literal}; impl Gate for Evil {{}}"
                masked = _mask_non_code(src)
                self.assertEqual(
                    masked.strip(), f"let c = {' ' * len(literal)}; impl Gate for Evil {{}}".strip()
                )

    def test_a_lifetime_is_code_not_a_literal(self) -> None:
        src = "fn g<'a>(x: &'a str) -> &'static str { x }"
        self.assertEqual(_mask_non_code(src), src)

    def test_a_span_that_never_closes_is_refused(self) -> None:
        """Unreadable is not empty (CLAUDE.md rule 3).

        Each of these used to blank to end of file or fall through to a guess,
        and either way the scan went on to report what it could still see.
        """
        for label, src in {
            "block comment": "pub fn a() {}\n/* never closed\nimpl Gate for Evil {}\n",
            "nested block comment": "/* outer /* inner */ still open\nimpl Gate for Evil {}\n",
            "string": 'pub const S: &str = "never closed;\nimpl Gate for Evil {}\n',
            "raw string": 'pub const S: &str = r#"never closed";\nimpl Gate for Evil {}\n',
            "char literal": "let c = '\\u{7b;\nimpl Gate for Evil {}\n",
        }.items():
            with self.subTest(shape=label):
                with self.assertRaises(ConnectivityCheckError) as ctx:
                    _mask_non_code(src)
                self.assertIn("unterminated", str(ctx.exception))

    def test_code_only_keeps_code_after_a_block_comment_closes(self) -> None:
        """`_code_only` deleted every line starting with `//`.

        Inside a block comment that line is still comment text up to its `*/`,
        and the code after it is real: `/*\n// */ impl Gate for Evil {}` compiles
        to an implementor, and the line-dropping stripper hid it from every scan
        that read through it - including the implementor enumeration.
        """
        src = "/*\n// */ impl Gate for Evil {}\n// a real line comment: impl Gate for Fake\n"
        code = _code_only(src)
        self.assertIn("impl Gate for Evil {}", code)
        self.assertNotIn("Fake", code)
        self.assertEqual(len(code), len(src))

    def test_code_only_keeps_string_contents(self) -> None:
        # The forbidden-token scans read string literals as code on purpose; only
        # comments are prose.
        self.assertIn(
            "to_socket_addrs", _code_only('let s = "to_socket_addrs"; // to_socket_addrs')
        )
        self.assertEqual(_code_only('let s = "x"; // y').count("y"), 0)


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
        # The real workspace's shape: every member under `crates/`.
        (self.tmp / "Cargo.toml").write_text(
            '[workspace]\nresolver = "2"\nmembers = ["crates/*"]\n', encoding="utf-8"
        )

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

    def test_a_semicolon_in_the_header_does_not_hide_an_implementor(self) -> None:
        """A backstop bounded like the pattern it backs up is not a backstop.

        History: the regex pair this pinned was replaced by a depth walker in r27.
        The shape stays pinned because the walker must still catch it.

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

    def test_an_ordinary_generic_bound_is_not_an_implementor(self) -> None:
        """The false-positive direction (first found in the r24 regex backstop).

        Counting any `impl ... Trait ... for` span made `impl<W:
        RestartWindowGate> SomeOtherTrait for Manager<W>` - a perfectly ordinary
        bound, not an implementation - look like an implementor header the
        strict pass could never match, so the whole connectivity gate failed on
        legal code. A backstop that cries wolf is deleted by the next person who
        meets it, which makes it as dead as one that never fires.
        """
        self._inject(
            "atp-orchestrator",
            """
pub struct Manager<W>(W);

impl<W: atp_market_data::RestartWindowGate> SomeOtherTrait for Manager<W> {
    fn unrelated(&self) {}
}
""",
        )
        evidence = check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("exactly 1 production type", evidence)

    def test_generics_on_the_TRAIT_do_not_hide_an_implementor(self) -> None:
        """`impl Gate<Clock = SystemClock> for X` - generics on the trait itself.

        Both the strict pattern and the backstop required a bare trait name
        before `for`, so this yielded expected == matched == 0 and read as a
        clean, closed set rather than as unparseable. The backstop exists to
        turn "cannot read" into red; it cannot do that for a shape it also
        cannot read.
        """
        for label, header in {
            "type argument": (
                "impl atp_market_data::RestartWindowGate<SystemClock> for AlwaysOpen {"
            ),
            "associated binding": (
                "impl atp_market_data::RestartWindowGate<Clock = SystemClock> for AlwaysOpen {"
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

    def test_a_scan_that_finds_nothing_fails_rather_than_reporting_clean(self) -> None:
        """An empty tree is the failure mode a scan-based guard dies of."""
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)
        (empty / "crates").mkdir()
        with self.assertRaises(ConnectivityCheckError):
            check_restart_window_gate_implementors(self.config, "", root=empty)

    def test_a_brace_inside_the_generics_does_not_hide_an_implementor(self) -> None:
        """Adversarial review r27 [high] - the reviewer's own reproducer, and its kin.

        Rust permits a `{` inside a const-generic argument. Both regex scans - the
        strict match and the backstop - assumed no brace could precede the trait
        name, so both missed the impl, their counts agreed at zero, and the scan
        reported exactly one approved implementor. The reviewer type-checked the
        first shape below. The walker bounds a header by bracket depth instead.
        """
        shapes = {
            "const generic in a bound": (
                "impl<T: HasWidth<{ 1 + 1 }>> atp_market_data::RestartWindowGate "
                "for AlwaysOpen<T> {"
            ),
            "a `>` inside the const brace": (
                "impl<T: HasWidth<{ 2 > 1 }>> atp_market_data::RestartWindowGate "
                "for AlwaysOpen<T> {"
            ),
            "const generic in the target": (
                "impl atp_market_data::RestartWindowGate for AlwaysOpen<{ 1 + 1 }> {"
            ),
        }
        for label, header in shapes.items():
            with self.subTest(shape=label):
                self._inject(
                    "atp-orchestrator",
                    f"""
pub struct AlwaysOpen<T>(T);

{header}
    fn admission(&self) -> MarketDataAdmission {{
        MarketDataAdmission::Admitted
    }}
}}
""",
                )
                with self.assertRaises(ConnectivityCheckError) as ctx:
                    check_restart_window_gate_implementors(self.config, "", root=self.tmp)
                self.assertIn("AlwaysOpen", str(ctx.exception))

    def test_a_macro_that_takes_the_trait_as_an_argument_fails_closed(self) -> None:
        # `gen!(atp_market_data::RestartWindowGate, AlwaysOpen)` would implement the
        # gate under a name no scan of the macro's definition can see.
        self._inject(
            "atp-orchestrator",
            """
macro_rules! gen {
    ($tr:path, $t:ty) => {
        impl $tr for $t {
            fn admission(&self) -> MarketDataAdmission { MarketDataAdmission::Admitted }
        }
    };
}
""",
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("metavariable", str(ctx.exception))

    def test_a_header_that_never_ends_fails_closed(self) -> None:
        self._inject("atp-orchestrator", "impl<T> atp_market_data::RestartWindowGate for X<T>")
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("end of file", str(ctx.exception))

    def test_a_comment_the_lexer_misreads_does_not_hide_an_implementor(self) -> None:
        """Adversarial review r28 [high] - the reviewer's reproducer, and its kin.

        The reviewer's shape passed all 13 static checks with exactly one approved
        implementor reported. Each shape here makes a comment end somewhere other
        than where the old lexer thought it did.
        """
        shapes = {
            "nested comment, then a quote": '/* outer /* inner */ " */',
            "nested comment, then a raw-string opener": '/* outer /* inner */ r#" */',
            "a `//` line inside a block comment": "/*\n// */",
        }
        for label, comment in shapes.items():
            with self.subTest(shape=label):
                self._inject(
                    "atp-orchestrator",
                    f"""
pub struct AlwaysOpen;
{comment}
impl atp_market_data::RestartWindowGate for AlwaysOpen {{
    fn admission(&self) -> MarketDataAdmission {{
        MarketDataAdmission::Admitted
    }}
}}
// "
""",
                )
                with self.assertRaises(ConnectivityCheckError) as ctx:
                    check_restart_window_gate_implementors(self.config, "", root=self.tmp)
                self.assertIn("AlwaysOpen", str(ctx.exception))

    def test_a_rename_in_any_use_tree_shape_does_not_hide_an_implementor(self) -> None:
        """Adversarial review r29 [high] - the reviewer's reproducer, and its kin.

        The alias finder read a braced group with `\\{([^}]*)\\}`, so a NESTED group
        ended the match early and the rename was never seen: `impl G for
        AlwaysOpen` passed all 13 checks with one approved implementor reported.
        A rename `X as Y` can only appear in a use declaration, which always ends
        at its first `;`, so the finder now reads every `X as Y` in that span at
        any depth.
        """
        shapes = {
            "the reviewer's: nested group after the rename": (
                "use atp_market_data::{RestartWindowGate as G, live_feed::{LiveFeedLoop}};"
            ),
            "nested group before the rename": (
                "use atp_market_data::{live_feed::{LiveFeedLoop}, RestartWindowGate as G};"
            ),
            "three levels deep": "use atp_market_data::{a::{b::{c}}, x::{RestartWindowGate as G}};",
            "pub, leading `::` and `self`": (
                "pub use ::atp_market_data::{self, RestartWindowGate as G};"
            ),
            "spread over lines, with comments": (
                "use atp_market_data::{\n    // the gate\n    RestartWindowGate /* renamed */ as G,\n};"
            ),
            "a raw-identifier alias": "use atp_market_data::RestartWindowGate as r#G;",
            "`self` renames the group's parent": (
                "use atp_market_data::RestartWindowGate::{self as G};"
            ),
            "`self` in a nested group": (
                "use atp_market_data::{live_feed, RestartWindowGate :: { self as G }};"
            ),
        }
        for label, use in shapes.items():
            with self.subTest(shape=label):
                self._inject(
                    "atp-orchestrator",
                    f"""
{use}
pub struct AlwaysOpen;
impl G for AlwaysOpen {{
    fn admission(&self) -> MarketDataAdmission {{
        MarketDataAdmission::Admitted
    }}
}}
""",
                )
                with self.assertRaises(ConnectivityCheckError) as ctx:
                    check_restart_window_gate_implementors(self.config, "", root=self.tmp)
                self.assertIn("AlwaysOpen", str(ctx.exception))

    def test_a_rename_through_a_macro_metavariable_fails_closed(self) -> None:
        # `m!(G)` would bind the gate to a name only the call site knows, and a
        # gate named through `$t` cannot be ruled out. Unboundable is not absent.
        for label, body in {
            "the alias is a metavariable": (
                "macro_rules! m { ($n:ident) => { use atp_market_data::RestartWindowGate as $n; }; }"
            ),
            "the renamed item is a metavariable": (
                "macro_rules! m { ($t:ident) => { use atp_market_data::$t as G; }; }"
            ),
        }.items():
            with self.subTest(shape=label):
                self._inject("atp-orchestrator", body)
                with self.assertRaises(ConnectivityCheckError) as ctx:
                    check_restart_window_gate_implementors(self.config, "", root=self.tmp)
                self.assertIn("metavariable", str(ctx.exception))

    def test_renames_of_other_items_in_nested_groups_are_not_implementors(self) -> None:
        # The legal direction: the finder may not cry wolf on ordinary imports.
        self._inject(
            "atp-orchestrator",
            """
use std::{fmt::{self as f, Display as Show}, io::{Result as IoResult}};
macro_rules! cast { ($x:expr) => { $x as u64 }; }
pub struct Real;
impl Show for Real {
    fn fmt(&self, out: &mut f::Formatter<'_>) -> f::Result { Ok(()) }
}
""",
        )
        evidence = check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("exactly 1 production type", evidence)

    def test_code_the_scan_cannot_see_fails_closed(self) -> None:
        """The enumeration's claim covers every COMPILED production source.

        It reads `crates/*/src/**/*.rs`, and Rust compiles code from elsewhere:
        `#[path]` points a module at any file, `include!` splices one in, a build
        script generates code, a Cargo target can live outside `src/`, a
        proc-macro crate can emit an impl built from strings, and a workspace
        member can sit outside `crates/`. An implementor in any of those was
        invisible while the scan reported one approved implementor. Each is now
        refused: none is used by the real tree, and a scan cannot bound what it
        cannot read.
        """
        crate = self.tmp / "crates" / "atp-orchestrator"
        cases = {
            "a #[path] module": (
                "src/sneaky.rs",
                '#[path = "../../../elsewhere.rs"]\nmod hidden;\n',
            ),
            "an include!": ("src/sneaky.rs", 'include!("../../../elsewhere.rs");\n'),
            "a path inside cfg_attr": (
                "src/sneaky.rs",
                '#[cfg_attr(not(test), path = "../../../elsewhere.rs")]\nmod hidden;\n',
            ),
            "include! under another name": (
                "src/sneaky.rs",
                'use std::include as splice;\nsplice!("../../../elsewhere.rs");\n',
            ),
            "a build script": ("build.rs", "fn main() {}\n"),
            "a build key": ("Cargo.toml", '[package]\nname = "x"\nbuild = "gen.rs"\n'),
            "a bin outside src": (
                "Cargo.toml",
                '[package]\nname = "x"\n[[bin]]\nname = "e"\npath = "bin/e.rs"\n',
            ),
            "a lib outside src": ("Cargo.toml", '[package]\nname = "x"\n[lib]\npath = "lib.rs"\n'),
            "a proc-macro crate": (
                "Cargo.toml",
                '[package]\nname = "x"\n[lib]\nproc-macro = true\n',
            ),
            "a member outside crates/": (
                "../../Cargo.toml",
                '[workspace]\nmembers = ["crates/*", "tools/evil"]\n',
            ),
            "a path dependency outside crates/": (
                "Cargo.toml",
                '[package]\nname = "x"\n[dependencies]\nevil = { path = "../../evil" }\n',
            ),
            "a target-specific path dependency": (
                "Cargo.toml",
                "[package]\nname = \"x\"\n[target.'cfg(unix)'.dependencies]\n"
                'evil = { path = "../../evil" }\n',
            ),
            "a git dependency": (
                "Cargo.toml",
                '[package]\nname = "x"\n[dependencies]\nevil = { git = "https://example.invalid/e" }\n',
            ),
            "a patch section": (
                "../../Cargo.toml",
                '[workspace]\nmembers = ["crates/*"]\n[patch.crates-io]\nserde = { path = "vendor/serde" }\n',
            ),
            "a workspace dependency outside crates/": (
                "../../Cargo.toml",
                '[workspace]\nmembers = ["crates/*"]\n[workspace.dependencies]\nevil = { path = "evil" }\n',
            ),
            "a root package": (
                "../../Cargo.toml",
                '[package]\nname = "root"\n[workspace]\nmembers = ["crates/*"]\n',
            ),
        }
        root_toml = (self.tmp / "Cargo.toml").read_text(encoding="utf-8")
        for label, (rel, text) in cases.items():
            with self.subTest(shape=label):
                path = (crate / rel).resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                try:
                    with self.assertRaises(ConnectivityCheckError) as ctx:
                        check_restart_window_gate_implementors(self.config, "", root=self.tmp)
                    self.assertIn("this scan cannot see", str(ctx.exception))
                finally:
                    if path == (self.tmp / "Cargo.toml").resolve():
                        path.write_text(root_toml, encoding="utf-8")
                    else:
                        path.unlink()

    def test_a_macro_handed_the_gate_fails_closed(self) -> None:
        """A macro given the trait's NAME can implement it where no scan looks.

        `#[derive(Delegate)] #[delegate(RestartWindowGate)]` (ambassador-style) or
        `gen_gate!(RestartWindowGate, AlwaysOpen)` expands to an implementor that
        appears nowhere in the source. The production tree never hands the
        gate's name to a macro, so any that does is refused - under an alias too.
        """
        for label, body in {
            "an attribute": (
                "#[derive(Delegate)]\n#[delegate(atp_market_data::RestartWindowGate)]\n"
                "pub struct AlwaysOpen(Inner);\n"
            ),
            "a function-like macro": "gen_gate!(atp_market_data::RestartWindowGate, AlwaysOpen);\n",
            "a braced macro under an alias": (
                "use atp_market_data::RestartWindowGate as G;\ngen_gate! { G => AlwaysOpen }\n"
            ),
            "the name as a string in an attribute": (
                '#[delegate(target = "atp_market_data::RestartWindowGate")]\n'
                "pub struct AlwaysOpen(Inner);\n"
            ),
            "the name as a string in a macro": 'gen_gate!("RestartWindowGate", AlwaysOpen);\n',
        }.items():
            with self.subTest(shape=label):
                self._inject("atp-orchestrator", body)
                with self.assertRaises(ConnectivityCheckError) as ctx:
                    check_restart_window_gate_implementors(self.config, "", root=self.tmp)
                self.assertIn("macro", str(ctx.exception))

    def test_macros_and_includes_that_do_not_name_the_gate_are_legal(self) -> None:
        self._inject(
            "atp-orchestrator",
            """
#[derive(Debug, Clone)]
pub struct Plain;
pub const SCHEMA: &str = include_str!("schema.json");
pub fn takes(gate: impl atp_market_data::RestartWindowGate) -> String {
    format!("{:?}", vec![1, 2])
}
""",
        )
        evidence = check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("exactly 1 production type", evidence)

    ALWAYS_ADMITS = (
        "    fn admission(&self) -> MarketDataAdmission {\n"
        "        MarketDataAdmission::Admitted\n"
        "    }\n"
    )

    def test_a_second_type_with_the_producers_name_is_not_the_producer(self) -> None:
        """Adversarial review r30 [high] - identity, not spelling.

        Implementors were recorded by BARE type name, so a different type that
        shares the producer's name - `bypass::ScheduledRestartConnectivity` - was
        filed under the producer's allowlist entry, and all 13 checks passed with
        "exactly one" implementor reported. Every impl now counts as its own
        occurrence, a target must be the bare type name, and the declared
        producer must be implemented exactly once, in its declared file.
        """
        gate = "atp_market_data::RestartWindowGate"
        sneaky = "crates/atp-market-data/src/sneaky.rs"
        shapes = {
            "the reviewer's: a module-qualified namesake": (
                sneaky,
                "mod bypass { pub struct ScheduledRestartConnectivity; }\n"
                f"impl {gate} for bypass::ScheduledRestartConnectivity {{\n"
                f"{self.ALWAYS_ADMITS}}}\n",
            ),
            "a bare namesake in another file": (
                sneaky,
                "pub struct ScheduledRestartConnectivity;\n"
                f"impl {gate} for ScheduledRestartConnectivity {{\n{self.ALWAYS_ADMITS}}}\n",
            ),
            "a namesake in an inline module of the producer's own file": (
                self.PRODUCER,
                "mod bypass {\n    pub struct ScheduledRestartConnectivity;\n"
                f"    impl {gate} for ScheduledRestartConnectivity {{\n{self.ALWAYS_ADMITS}    }}\n}}\n",
            ),
            "a crate-qualified target": (
                sneaky,
                f"impl {gate} for crate::ScheduledRestartConnectivity {{\n{self.ALWAYS_ADMITS}}}\n",
            ),
        }
        for label, (rel, body) in shapes.items():
            with self.subTest(shape=label):
                path = self.tmp / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                before = path.read_text(encoding="utf-8") if path.exists() else None
                path.write_text((before or "") + "\n" + body, encoding="utf-8")
                try:
                    with self.assertRaises(ConnectivityCheckError) as ctx:
                        check_restart_window_gate_implementors(self.config, "", root=self.tmp)
                    self.assertIn("ScheduledRestartConnectivity", str(ctx.exception))
                finally:
                    if before is None:
                        path.unlink()
                    else:
                        path.write_text(before, encoding="utf-8")

    def test_the_producer_file_implementing_a_namesake_is_refused(self) -> None:
        """Identity is the WHOLE target, so the one impl at the site must be the producer.

        Keyed by the last path segment, `impl Gate for bypass::ScheduledRestartConnectivity`
        written AS the site's only impl would read as "the producer, once, at its site"
        while the real type implemented nothing.
        """
        producer = self.tmp / self.PRODUCER
        source = producer.read_text(encoding="utf-8")
        real = "atp_market_data::RestartWindowGate for ScheduledRestartConnectivity<P, C>"
        self.assertEqual(source.count(real), 1, "the producer's header moved; update this test")
        producer.write_text(
            source.replace(real, real.replace(" for ", " for bypass::"))
            + "\nmod bypass { pub struct ScheduledRestartConnectivity<P, C>(P, C); }\n",
            encoding="utf-8",
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("bypass::ScheduledRestartConnectivity", str(ctx.exception))

    def test_the_producer_must_be_implemented_at_its_declared_site(self) -> None:
        config = json.loads(json.dumps(self.config))
        gate = config["connectivity_contract"]["restart_window"]["gate_port"]
        gate["production_implementor_sites"]["ScheduledRestartConnectivity"] = (
            "crates/atp-orchestrator/src/elsewhere.rs"
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_restart_window_gate_implementors(config, "", root=self.tmp)
        self.assertIn("elsewhere.rs", str(ctx.exception))

    def test_a_nested_comment_that_closes_properly_is_not_an_implementor(self) -> None:
        # The legal direction: commented-out code is not code, at any depth.
        self._inject(
            "atp-orchestrator",
            """
/* outer /* inner */
impl atp_market_data::RestartWindowGate for Commented {}
*/
pub struct Real;
""",
        )
        evidence = check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("exactly 1 production type", evidence)
        self.assertNotIn("Commented", evidence)

    def test_legal_code_that_only_MENTIONS_the_gate_is_not_an_implementor(self) -> None:
        """The other direction: a walker that cries wolf is deleted by the next reader.

        Each shape is legal production code that names the gate without
        implementing it: text in a string literal, `impl Trait` in an argument
        position next to a where clause's commas, and a return-position
        `impl Trait`. None may count, and none may fail the scan.
        """
        self._inject(
            "atp-orchestrator",
            """
pub const DOC: &str = "impl RestartWindowGate for Fake {";

pub fn takes(gate: impl atp_market_data::RestartWindowGate, n: u8) -> u8
where
    u8: Copy,
    u16: Copy,
{
    n
}

pub fn gives<'a>() -> impl Fn() -> u8 + 'a {
    || 1
}
""",
        )
        evidence = check_restart_window_gate_implementors(self.config, "", root=self.tmp)
        self.assertIn("exactly 1 production type", evidence)
        self.assertNotIn("Fake", evidence)


if __name__ == "__main__":
    # Kept at the END of the file on purpose. It previously sat mid-file, ahead
    # of the SRS-MD-005 guard classes, so `python3 tests/test_connectivity_contract.py`
    # exited 0 having executed none of them — a green covering nothing the
    # feature added. (CI runs pytest, which collects the whole module either
    # way, so nothing was red; that is exactly what made it easy to miss.)
    unittest.main()
