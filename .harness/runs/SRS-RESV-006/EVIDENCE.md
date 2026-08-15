# SRS-RESV-006 — verification evidence

> Verify that enforce Hot-Swap cool-down behavior.

- **method**: `e2e`
- **steps evidenced**: 4/4
- **critics**: deterministic `approve`
- **artifacts**: 10
- **record complete**: NO

## Outstanding

- no judgment critic verdict recorded

## Acceptance criterion

> Step 3: Verify acceptance criteria: After successful swap, automatic triggers are ignored for the configured cool-down period defaulting to 7 calendar days; manual swap during cool-down requires confirmation warning; the cool-down start time is the timestamp of the most recent successful swap completion.

## Steps

### Step 1

Step 1: Run ./init.sh and confirm the development environment reports Environment ready before verification begins.

`pass` · exit `0` · executed by the tool

```
./init.sh
```

<details><summary>observed output (tail)</summary>

```
→ Installing dependencies...
  Using /Users/joshgenao/Documents/Programming/Python/alphalabs-wt-SRS-RESV-006/.venv/bin/python (Python 3.13)
→ Starting dev server...
  Dev server already running at http://127.0.0.1:3000.
→ Waiting for server...
→ Running baseline smoke test...
→ Running contract checks (scope=env)...
→ contract checks (scope=env, 17 check(s))
  · architecture_check
  · dependency_boundary_check
  · config_check
  · startup_readiness_gate_check
  · startup_readiness_runtime_check
  · ib_adapter_check
  · rest_api_check
  · websocket_api_check
  · cli_check
  · operator_workflow_surface_check
  · operator_interface_runtime_check
  · log_record_check
  · log_persistence_check
  · subscription_fanout_check
  · sim_halt_check
  · strategy_api_indicators_check
  · strategy_api_documentation_check
✓ 17/17 contract check(s) passed (scope=env)
✓ Environment ready
```

</details>

### Step 2

Step 2: Exercise SRS-RESV-006 using browser automation against the dashboard plus REST or WebSocket checks where applicable with the fixtures, mocks, or operator controls needed by the requirement.

`pass` · exit `0` · executed by the tool

```
.venv/bin/python -m pytest tests/e2e/test_hot_swap_cooldown_browser.py -q
```

<details><summary>observed output (tail)</summary>

```
..                                                                       [100%]
2 passed in 13.54s
```

</details>

![Hot-Swap pane with no cool-down window: the dial reads READY and the promote control is armable (SYS-49e clause 1, baseline)](artifacts/step2-01-ymce5v.png)

*Hot-Swap pane with no cool-down window: the dial reads READY and the promote control is armable (SYS-49e clause 1, baseline)*

![A recorded swap completion opens the SYS-49e window: the dial reads ACTIVE with the remaining time counted from the completion timestamp (clause 1, and clause 3's start time)](artifacts/step2-02-ymce5v.png)

*A recorded swap completion opens the SYS-49e window: the dial reads ACTIVE with the remaining time counted from the completion timestamp (clause 1, and clause 3's start time)*

![Arming during the cool-down raises SYS-49e's confirmation warning — the manual swap is offered, not blocked (clause 2)](artifacts/step2-03-ymce5v.png)

*Arming during the cool-down raises SYS-49e's confirmation warning — the manual swap is offered, not blocked (clause 2)*

![After the confirmed swap: the window has RESTARTED at the new swap's own completion timestamp, so the countdown is back to nearly seven days (clause 3 — the start time is the most recent successful completion)](artifacts/step2-04-ymce5v.png)

*After the confirmed swap: the window has RESTARTED at the new swap's own completion timestamp, so the countdown is back to nearly seven days (clause 3 — the start time is the most recent successful completion)*

🎬 [full session recording](artifacts/step2-page@50c099c658b0785ed9f3c079aa4dc5a9.webm) — 1.0 MB. GitHub does not play video from a repo path; download it, or open the PR's CI artifact.

![A corrupt cool-down window: the dial reports UNKNOWN and the promote control is held inert — an unreadable window is never read as 'clear'](artifacts/step2-01-4stxxv.png)

*A corrupt cool-down window: the dial reports UNKNOWN and the promote control is held inert — an unreadable window is never read as 'clear'*

🎬 [full session recording](artifacts/step2-page@d118a25547eeb2fa4a8862200c069029.webm) — 0.3 MB. GitHub does not play video from a repo path; download it, or open the PR's CI artifact.

![UI-5 pane, real browser: an ACTIVE SYS-49e window (6d 22h remaining, since/expires from the durable store) and the promote control armed with the cool-down confirmation warning — the AC's second clause, rendered](artifacts/step2-step2-03-ymce5v.png)

*UI-5 pane, real browser: an ACTIVE SYS-49e window (6d 22h remaining, since/expires from the durable store) and the promote control armed with the cool-down confirmation warning — the AC's second clause, rendered*


### Step 3

Step 3: Verify acceptance criteria: After successful swap, automatic triggers are ignored for the configured cool-down period defaulting to 7 calendar days; manual swap during cool-down requires confirmation warning; the cool-down start time is the timestamp of the most recent successful swap completion.

`pass` · exit `0` · executed by the tool

```
.venv/bin/python -m pytest tests/domain/test_hot_swap_cooldown.py -q
```

<details><summary>observed output (tail)</summary>

```
...............................                                          [100%]
31 passed in 2.30s
```

</details>

![The AC's third clause, visible: after the swap completed ("promoted reservoir-b live · swap sw-1") the dial's start moved to 2026-08-15T13:37:22Z — the timestamp of THIS completion, not of the earlier window (12:37:12Z in the armed shot). The seven-day expiry moved with it.](artifacts/step3-step2-04-ymce5v.png)

*The AC's third clause, visible: after the swap completed ("promoted reservoir-b live · swap sw-1") the dial's start moved to 2026-08-15T13:37:22Z — the timestamp of THIS completion, not of the earlier window (12:37:12Z in the armed shot). The seven-day expiry moved with it.*

![The AC's first clause: the three automatic triggers (drawdown demotion, top-ranked promotion, highest-momentum promotion) render off/suppressed while the window is in effect, and manual promotion stays 'always available' per SYS-49a(a).](artifacts/step3-step2-01-ymce5v.png)

*The AC's first clause: the three automatic triggers (drawdown demotion, top-ranked promotion, highest-momentum promotion) render off/suppressed while the window is in effect, and manual promotion stays 'always available' per SYS-49a(a).*


### Step 4

Step 4: Record objective evidence from test and leave passes false until the evidence proves the requirement end to end.

`pass` · exit `0` · executed by the tool

```
cargo test -p atp-orchestrator
```

<details><summary>observed output (tail)</summary>

```
t.rs (target/debug/deps/orch_1_lifecycle_contract-7831891b84ee895a)
     Running tests/orch_2_resource_profile_contract.rs (target/debug/deps/orch_2_resource_profile_contract-7c122c85bc25ed55)
     Running tests/orch_3_workload_priority_contract.rs (target/debug/deps/orch_3_workload_priority_contract-b2c0a21127794d21)
     Running tests/orch_4_deployment_version_contract.rs (target/debug/deps/orch_4_deployment_version_contract-726fde9b617eabaa)
     Running tests/orch_5_cli_fail_closed.rs (target/debug/deps/orch_5_cli_fail_closed-3a77e6ab502734fb)
     Running tests/orch_5_rollback_contract.rs (target/debug/deps/orch_5_rollback_contract-86401f2bfa275bcb)
     Running tests/resv_3_cli_fail_closed.rs (target/debug/deps/resv_3_cli_fail_closed-800e942f509af1e4)
resv003_hot_swap_trigger_cli: SRS-RESV-006: manual Hot-Swap requires confirmation during a cool-down (window UNKNOWN): the Hot-Swap cool-down window could not be determined, so this build cannot say whether a swap is within one (SyRS SYS-49e): no cool-down state path configured (--cooldown-state); this build cannot say whether a Hot-Swap cool-down is in effect. Confirm to swap manually anyway.
resv003_hot_swap_trigger_cli: degraded input port(s): hot-swap cool-down state: no cool-down state path configured (--cooldown-state); this build cannot say whether a Hot-Swap cool-down is in effect (fail closed)
resv003_hot_swap_trigger_cli: degraded input port(s): hot-swap cool-down state: no cool-down state path configured (--cooldown-state); this build cannot say whether a Hot-Swap cool-down is in effect (fail closed)
resv003_hot_swap_trigger_cli: SRS-RESV-006: manual Hot-Swap requires confirmation during a cool-down (window UNKNOWN): the Hot-Swap cool-down window could not be determined, so this build cannot say whether a swap is within one (SyRS SYS-49e): no cool-down state path configured (--cooldown-state); this build cannot say whether a Hot-Swap cool-down is in effect. Confirm to swap manually anyway.
     Running tests/resv_3_hot_swap_triggers.rs (target/debug/deps/resv_3_hot_swap_triggers-61209c7189580bd8)
     Running tests/resv_3_trigger_log_schema.rs (target/debug/deps/resv_3_trigger_log_schema-93b6e6aaf812c4e8)
     Running tests/resv_4_demotion_pending_store.rs (target/debug/deps/resv_4_demotion_pending_store-8ff60a7498efb2ef)
     Running tests/resv_4_demotion_sequence.rs (target/debug/deps/resv_4_demotion_sequence-e8671bc2caea6efe)
     Running tests/resv_5_hot_swap_promotion.rs (target/debug/deps/resv_5_hot_swap_promotion-9c1d1ab4ccc457f4)
     Running tests/resv_6_cli_fail_closed.rs (target/debug/deps/resv_6_cli_fail_closed-69514517cd57f634)
     Running tests/resv_6_cooldown_classification.rs (target/debug/deps/resv_6_cooldown_classification-a4cb1b2c83a09735)
     Running tests/resv_6_cooldown_execution.rs (target/debug/deps/resv_6_cooldown_execution-c721ff6e6d50e3ec)
     Running tests/resv_6_cooldown_gate.rs (target/debug/deps/resv_6_cooldown_gate-42d0c6677a6e34fb)
     Running tests/resv_6_cooldown_relative_path.rs (target/debug/deps/resv_6_cooldown_relative_path-ac52d07bb6e1721a)
     Running tests/resv_6_cooldown_store.rs (target/debug/deps/resv_6_cooldown_store-c78f6b09b0f05d50)
     Running tests/safe_002_liquidation_timeout_scenario.rs (target/debug/deps/safe_002_liquidation_timeout_scenario-c9b98b2df890067c)
     Running tests/srs_err_001_broker_envelope_cli.rs (target/debug/deps/srs_err_001_broker_envelope_cli-f9208b28391a2650)
     Running tests/srs_err_001_broker_envelope_live.rs (target/debug/deps/srs_err_001_broker_envelope_live-c072c7b3bd170949)
     Running tests/srs_exe_002_routing_wiring.rs (target/debug/deps/srs_exe_002_routing_wiring-04db9f6704493cd1)
     Running tests/srs_safe_003_connectivity_block_cli.rs (target/debug/deps/srs_safe_003_connectivity_block_cli-32507a0b483a7ec5)
     Running tests/srs_safe_003_connectivity_block_wiring.rs (target/debug/deps/srs_safe_003_connectivity_block_wiring-442cff82d0507d35)
   Doc-tests atp_orchestrator
```

</details>

---

Generated by `tools/evidence.py render`. `passes: true` requires either every step executed by the tool with both critics approving, or a named human attestation — see `AGENTS.md`.
