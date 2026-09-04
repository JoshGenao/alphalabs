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


if __name__ == "__main__":
    unittest.main()


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

    def test_both_admission_sites_are_gated(self) -> None:
        evidence = check_market_data_admission_sites(self.config, self.market_data_src)
        self.assertIn("request_subscription", evidence)
        self.assertIn("subscribe", evidence)

    def test_an_ungated_admission_site_is_caught(self) -> None:
        # Remove the guard from the MUTATING admission point. The function still
        # takes the port, so a checklist naming "these two functions accept a
        # window" would still pass — only reading the body catches it.
        marker = "        match window.admission() {"
        self.assertEqual(
            self.market_data_src.count(marker),
            2,
            "expected exactly two admission guards; the anchor has drifted",
        )
        last = self.market_data_src.rindex(marker)
        mutated = self.market_data_src[:last] + self.market_data_src[last:].replace(
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
            check_market_data_admission_sites(self.config, self.market_data_src + bypass)
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
            check_market_data_admission_sites(config, self.market_data_src + bypass)
        self.assertIn("without calling", str(ctx.exception))

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
            check_market_data_admission_sites(self.config, self.market_data_src + wrapped)
        self.assertIn("subscribe_wrapped", str(ctx.exception))

    def test_a_scan_that_matches_nothing_fails_rather_than_reporting_clean(self) -> None:
        # A broken discovery reports a clean tree, which is the failure mode
        # that looks most like the guard working.
        mutated = self.market_data_src.replace("SubscriptionAccepted {", "Renamed {").replace(
            "self.subscribers\n                .insert(", "self.renamed.insert("
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_market_data_admission_sites(self.config, mutated)
        self.assertIn("no function", str(ctx.exception))


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
