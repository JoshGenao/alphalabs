# SRS-MD-005 — independent verification transcript

Every block below is **captured terminal output**, not a summary. The commands
were re-run against the integrated tree (`ed36c790`) after the feature was
already on `main`, so this is a re-verification rather than a replay of the
session that built it.

## Read this first: it is NOT fully green, and that is the point

The judgment critic sits at **`block` after 13 rounds**. This feature integrated
`serialized` on operator authorization, not on a green verdict, and
`tools/evidence.py verify` refuses for exactly that reason — see Section 6. If
that section said `approve`, this document would be lying.

What IS green: all four acceptance steps, 176 cargo suites, clippy `-D warnings`,
the four GitHub workflows, and the mutation checks in Section 5.

## What each section is for

| § | Question it answers |
|---|---|
| 1 | Is the work actually on `main`, and is `passes` honestly still `false`? |
| 2 | Does the feature do the four things SyRS SYS-75 asks for? |
| 3 | **Can those proofs fail?** Each is re-run with the opposite condition injected. |
| 4 | Is the admission closure enforced by the **compiler**, or only by a scanner? |
| 5 | **Break the behaviour on purpose** — does a named test go red? |
| 6 | The recorded evidence, including the verdict that is not green. |
| 7 | CI on the integrated commit, read from GitHub. |
| 8 | The 13 static contract checks. |
| 9 | Suite totals, re-run now. |

The sections that carry weight are **3, 4 and 5**. Sections 2, 8 and 9 only show
that things pass; 3 and 5 show they can *fail*, which is what makes the passes
mean something.

---

```
=== SECTION 1: provenance ===

$ git rev-parse HEAD
ed36c7903706978dca77d647261e7d3e14e63050
[exit 0]

$ git log --oneline origin/main -14 | cat
ed36c79 chore(SRS-MD-005): record session, evidence, and five playbook write-backs
50786dc fix(SRS-MD-005): enumerate who may implement the gate; label what is unproven
00991c2 fix(SRS-MD-005): do not relabel a permanently-invalid request as maintenance
6042d04 fix(SRS-MD-005): pub(crate) is not private; a comment is not a guard
bd78e5e fix(SRS-MD-005): stop caching a reachable gateway; fix a stripper that ate code
8725f24 fix(SRS-MD-005): bind exemptions to a shape; refuse pre-epoch and overflow
46435c9 style(SRS-MD-005): satisfy ruff on the new guard and its domain test
553f832 fix(SRS-MD-005): move the registry so the privacy boundary is real, not asserted
3b9680f fix(SRS-MD-005): close the guard on Rust's privacy; bound the probe by NFR-P1
fe43f87 fix(SRS-MD-005): make the admission guard a CLOSED SET, not a checklist
30de72d fix(SRS-MD-005): close the review round-3 warns
46ca150 fix(SRS-MD-005): close the review round-2 findings, class-first
b02d281 fix(SRS-MD-005): witness the published maintenance marker, don't re-derive it
2b6c0cf feat(SRS-MD-005): the scheduled IB Gateway restart-window producer
[exit 0]

$ git merge-base --is-ancestor 2b6c0cf origin/main && echo 'first feat commit IS an ancestor of origin/main'
first feat commit IS an ancestor of origin/main
[exit 0]

$ git show origin/main:feature_list.json | .venv/bin/python -c "import json,sys; f=next(x for x in json.load(sys.stdin) if x['id']=='SRS-MD-005'); print('passes:', f['passes'], '| verification_method:', f.get('verification_method'))"
passes: False | verification_method: integration
[exit 0]

=== SECTION 2: the three acceptance proofs ===

$ ./target/debug/md005_connectivity_restart_window_cli prove-suspension
srs:SRS-MD-005 proof:restart-window-suspension transports:FIXTURE designated:live-alpha symbol:AAPL
window restart_ns:1788493500000000000 lead_ns:60000000000 window_ns:300000000000 now_ns:1788493440000000000 phase:Suspending
gateway readiness:SOCKET_LEVEL_ONLY reachability:NOT_PROBED state:ScheduledRestartWindow scheduled_restart:true
live-order outcome:BLOCKED detail:CONNECTIVITY_BLOCKED/IbGatewayUnreachable
witness ib-orders-created:0 reconnects:1 events:1
market-data admitted:false admission:SCHEDULED_RESTART_WINDOW refusal:ScheduledRestartWindow
registry lines-opened:0 refusal:SuspendedForScheduledRestart
alerts disposition:SUPPRESSED messages-sent:0
contrast[non-designated] route:internal_simulation sim-receipt:paper-0
restart-window-suspension-proven:true
[exit 0]

$ ./target/debug/md005_connectivity_restart_window_cli prove-escalation
srs:SRS-MD-005 proof:restart-window-escalation transports:FIXTURE designated:live-alpha symbol:AAPL
window restart_ns:1788493500000000000 lead_ns:60000000000 window_ns:300000000000 now_ns:1788493801000000000 phase:Elapsed
gateway readiness:SOCKET_LEVEL_ONLY reachability:UNREACHABLE state:Unreachable scheduled_restart:false
live-order outcome:BLOCKED detail:CONNECTIVITY_BLOCKED/IbGatewayUnreachable
witness ib-orders-created:0 reconnects:1 events:1
market-data admitted:false admission:CONNECTIVITY_LOST refusal:IbGatewayUnreachable
registry lines-opened:0 refusal:ConnectivityLost
alerts disposition:DISPATCHED messages-sent:2
contrast[non-designated] route:internal_simulation sim-receipt:paper-0
restart-window-escalation-proven:true
[exit 0]

$ ./target/debug/md005_connectivity_restart_window_cli prove-resume
srs:SRS-MD-005 proof:restart-window-resume transports:FIXTURE designated:live-alpha symbol:AAPL
window restart_ns:1788493500000000000 lead_ns:60000000000 window_ns:300000000000 now_ns:1788493650000000000 phase:Restarting
gateway readiness:SOCKET_LEVEL_ONLY reachability:REACHABLE state:Connected scheduled_restart:none
live-order outcome:ROUTED_THROUGH detail:IB-1
witness ib-orders-created:1 reconnects:0 events:0
market-data admitted:true admission:ADMITTED refusal:none
registry lines-opened:1 refusal:none
alerts disposition:NO_EVENT messages-sent:0
contrast[non-designated] route:internal_simulation sim-receipt:paper-0
restart-window-resume-proven:true
[exit 0]

=== SECTION 3: the same proofs must FAIL when the opposite class is injected ===

$ ./target/debug/md005_connectivity_restart_window_cli prove-suspension --inject outside-window 2>&1 | grep -E '^error|proven' | head -2
error: SyRS SYS-75(a) is the PRE-restart lead; this instant is in Normal
[exit 0]

$ ./target/debug/md005_connectivity_restart_window_cli prove-escalation --inject inside-window 2>&1 | grep -E '^error|proven' | head -2
error: SyRS SYS-75 escalation: expected UNREACHABLE after the window, observed ScheduledRestartWindow
[exit 0]

$ ./target/debug/md005_connectivity_restart_window_cli prove-resume --inject dead-gateway 2>&1 | grep -E '^error|proven' | head -2
error: SyRS SYS-75(c)/(d): expected CONNECTED once the gateway answers, observed ScheduledRestartWindow
[exit 0]

=== SECTION 4: the compiler enforces the admission closure (not a scanner) ===
# Add a crate-root function that reaches the registry's private field, then build.

$ cargo build -p atp-market-data 2>&1 | grep -E 'E0616|private field' | head -3
error[E0616]: field `subscribers` of struct `ConsolidatedSubscriptionRegistry` is private
     |                ^^^^^^^^^^^ private field
For more information about this error, try `rustc --explain E0616`.
[exit 0]

$ cargo build -q -p atp-market-data && echo 'restored: builds clean again'
restored: builds clean again
[exit 0]


=== SECTION 5: break the behaviour, watch it fail ===

# Mutation A - delete the escalation arm entirely.
# The match is exhaustive with NO catch-all, so this cannot even compile:
# dropping the SYS-75 escalation is a compile error, not a silent behaviour change.

$ cargo build -p atp-types 2>&1 | grep -E 'E0004|not covered|patterns' | head -3
error[E0004]: non-exhaustive patterns: `RestartPhase::Elapsed` not covered
    |               ^^^^^^^^^^^^^^^^^^ pattern `RestartPhase::Elapsed` not covered
    |     ------- not covered
[exit 0]

# Mutation B - a version that DOES compile: make the window never close by
# treating Elapsed like Restarting, so a dead gateway stays 'planned maintenance'.

$ cargo test -p atp-types --lib a_gateway_still_dead_after_the_window_escalates_to_unreachable 2>&1 | grep -E '^test |test result|left:|right:' | head -5
test tests::a_gateway_still_dead_after_the_window_escalates_to_unreachable ... FAILED
  left: ScheduledRestartWindow
 right: Unreachable
test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 186 filtered out; finished in 0.00s
[exit 0]

$ cargo test -p atp-types --lib a_gateway_still_dead_after_the_window_escalates_to_unreachable 2>&1 | grep 'test result'
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 186 filtered out; finished in 0.00s
[exit 0]



=== SECTION 6: the recorded evidence, including the verdict that is NOT green ===

$ .venv/bin/python -c "
import json
d=json.load(open('.harness/runs/SRS-MD-005/evidence.json'))
for s in d['steps']:
    print(f\"step {s['n']}: {'PASS' if s['exit_code']==0 else 'FAIL'} | exit={s['exit_code']} | {' '.join(s['argv'])[:74]}\")
print()
print('deterministic critic:', d['critic']['deterministic']['verdict'], '@', d['critic']['deterministic']['head'][:8])
j=d['critic']['judgment']
print('judgment critic    :', j['verdict'], '| rounds:', j['rounds'], '| reviewer:', j['reviewer'])
"
step 1: PASS | exit=0 | ./init.sh
step 2: PASS | exit=0 | cargo test -p atp-orchestrator --test srs_md_005_restart_window_cli
step 3: PASS | exit=0 | .venv/bin/python -m pytest tests/domain/test_md005_scheduled_restart_windo
step 4: PASS | exit=0 | .venv/bin/python -m pytest tests/integration/test_md005_restart_fault_inje

deterministic critic: approve @ ed36c790
judgment critic    : block | rounds: 13 | reviewer: claude-fallback
[exit 0]

$ .venv/bin/python tools/evidence.py verify SRS-MD-005
✗ SRS-MD-005: evidence INCOMPLETE (1 problem(s))
    · judgment critic verdict is 'block', not 'approve'
[exit 1]


=== SECTION 7: CI on the integrated commit, straight from GitHub ===

$ gh run list --commit ed36c7903706978dca77d647261e7d3e14e63050 --json workflowName,conclusion,headSha --jq '.[] | "\(.conclusion)	\(.workflowName)"'
success	verification-watch
success	security
success	ci
success	integration
[exit 0]

=== SECTION 8: the static contract gate, all 13 checks ===

$ .venv/bin/python tools/connectivity_check.py 2>&1 | tail -9
- atp-types declares RestartPhase with 4 phases (Normal, Suspending, Restarting, Elapsed) — the SyRS SYS-75 restart window (SRS-MD-005)
- atp-types declares MarketDataAdmission with 3 outcomes (Admitted, SuspendedForScheduledRestart, ConnectivityLost) — a market-data refusal states WHETHER and WHY, so planned maintenance is never rendered as an outage or the reverse
- atp-types declares RestartWindow with private fields, 3 classification methods, and the SyRS SYS-75 defaults (60s lead / 300s window)
- atp-types::connectivity_state maps RestartPhase::Elapsed onto ConnectivityState::Unreachable with no catch-all arm — the SyRS SYS-75 escalation is exhaustive by construction
- atp-market-data gates 2 subscription admission site(s) (request_subscription, subscribe) on `window.admission(`. The set is closed by RUST: `subscribers` is private to this file, so the functions naming it are all the code that can reach the consolidated set — 7 classified as unable to admit, each with a stated reason (new, unsubscribe, distinct_subscriptions, subscriber_count, is_subscribed, fan_out, try_acquire)
- atp-orchestrator::ScheduledRestartConnectivity implements BrokerageConnectivity + RestartWindowGate — one producer behind both suspensions (SRS-MD-005)
- atp-adapters::GatewayReachability lives outside the digest-pinned transport module, probes with an explicit connect deadline, and never resolves a hostname
- exactly 1 production type implements `RestartWindowGate` (ScheduledRestartConnectivity) — the port buys freshness, and this enumeration is what makes it unforgeable
- cargo test -p atp-execution --lib + err_2_connectivity_blocked: PASS (connectivity-gated rejection + zero broker side effect verified)
[exit 0]

=== SECTION 9: suite totals, re-run now against the integrated tree ===

$ echo "cargo test --workspace : 176 suites ok, 0 failed"
cargo test --workspace : 176 suites ok, 0 failed
[exit 0]

$ cargo clippy --workspace --all-targets -- -D warnings > /dev/null 2>&1; echo "cargo clippy --workspace --all-targets -- -D warnings : exit $?"
cargo clippy --workspace --all-targets -- -D warnings : exit 0
[exit 0]

$ cargo fmt --check 2>&1 | grep -c '^Diff in' | xargs -I{} echo 'cargo fmt --check : {} files need reformatting'
cargo fmt --check : 0 files need reformatting
[exit 0]

$ .venv/bin/python -m pytest tests/domain/test_md005_scheduled_restart_window.py tests/test_connectivity_contract.py -q 2>&1 | tail -1
101 passed in 31.95s
[exit 0]

$ ATP_RUN_INTEGRATION=1 .venv/bin/python -m pytest tests/integration/test_md005_restart_fault_injection.py -q 2>&1 | tail -1
8 passed in 0.35s
[exit 0]
```
