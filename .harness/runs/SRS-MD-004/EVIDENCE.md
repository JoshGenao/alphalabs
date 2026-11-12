# SRS-MD-004 — verification evidence

> Verify that block live and simulated order submission when required market data is stale.

- **method**: `solo`
- **steps evidenced**: 4/4
- **critics**: deterministic `approve`
- **artifacts**: 0
- **record complete**: NO

## Outstanding

- deterministic critic approved at e4e72239, but 2 non-evidence path(s) have changed since (architecture/runtime_services.json, crates/atp-orchestrator/src/market_data_freshness.rs) — the approval describes code this close would not ship. Re-run the deterministic review.
- no judgment critic verdict recorded

## Acceptance criterion

> Step 3: Verify acceptance criteria: While subscribed market data is stale, live and paper order submissions return MARKET_DATA_STALE; submissions resume when fresh data is received.

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
  Using /Users/joshgenao/Documents/Programming/Python/alphalabs-wt-SRS-MD-004/.venv/bin/python (Python 3.13)
→ Starting dev server...
  Dev server already running at http://127.0.0.1:3000.
→ Waiting for server...
→ Running baseline smoke test...
→ Running contract checks (scope=env)...
→ contract checks (scope=env, 18 check(s))
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
  · dependency_check
✓ 18/18 contract check(s) passed (scope=env)
✓ Environment ready
```

</details>

### Step 2

Step 2: Exercise SRS-MD-004 using CLI/API workflows with fixture market data, provider mocks, file reads, and persisted output inspection with the fixtures, mocks, or operator controls needed by the requirement.

`pass` · exit `0` · executed by the tool

```
cargo test -p atp-orchestrator --test srs_md_004_stale_gate_wired
```

<details><summary>observed output (tail)</summary>

```
running 2 tests
test c4_one_lines_fresh_data_does_not_admit_orders_on_another_line ... ok
test c4_live_and_paper_block_while_stale_and_resume_when_fresh_data_arrives ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

    Finished `test` profile [unoptimized + debuginfo] target(s) in 0.00s
     Running tests/srs_md_004_stale_gate_wired.rs (target/debug/deps/srs_md_004_stale_gate_wired-46b09ac8d0198f19)
```

</details>

### Step 3

Step 3: Verify acceptance criteria: While subscribed market data is stale, live and paper order submissions return MARKET_DATA_STALE; submissions resume when fresh data is received.

`pass` · exit `0` · executed by the tool

```
.venv/bin/python -m pytest tests/domain/test_stale_data_block.py -q
```

<details><summary>observed output (tail)</summary>

```
.............                                                            [100%]
13 passed in 0.50s
/Users/joshgenao/Documents/Programming/Python/alphalabs-wt-SRS-MD-004/.venv/lib/python3.13/site-packages/pytest_asyncio/plugin.py:300: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
```

</details>

### Step 4

Step 4: Record objective evidence from test and leave passes false until the evidence proves the requirement end to end.

`pass` · exit `0` · executed by the tool

```
.venv/bin/python tools/freshness_check.py
```

<details><summary>observed output (tail)</summary>

```
ERR-3 PASS
- atp-types declares MarketDataFreshness with 2 states (Fresh, Stale) — market-data freshness gate (SRS-MD-004 / NFR-P5)
- atp-types declares StaleDataEvent with the 4 required fields (state, strategy_id, symbol, staleness_seconds) and rejects 5 forbidden broker/vendor/tick fields
- atp-execution declares port trait MarketDataFreshnessProbe with 2 methods (freshness, staleness_seconds) — the SRS-MD-004 freshness probe (subscription manager port)
- atp-execution declares port trait StaleDataEventSink with 1 method (record) — the structured-event publication channel for ERR-3
- atp-execution::submit_live_order gates `broker.submit_order` on a nested match MarketDataFreshness::Fresh inside ConnectivityState::Connected; the MarketDataFreshness::Stale leaf emits OrderErrorCategory::MarketDataStale, records a StaleDataEvent via `stale_events.record`, and produces zero broker side effect (ERR-3)
- atp-execution::dispatch_order gates `simulation.submit_simulated` on MarketDataFreshness::Fresh inside OrderRoute::InternalSimulation; the MarketDataFreshness::Stale leaf emits OrderErrorCategory::MarketDataStale and records a StaleDataEvent via `stale_events.record` (SRS-MD-004 / SYS-87 paper half)
- cargo test -p atp-execution --lib + err_3_stale_data_blocked + srs_md_004_paper_stale_gate: PASS (live and paper stale-gated rejection, zero broker and simulation side effect verified)
```

</details>

---

Generated by `tools/evidence.py render`. `passes: true` requires either every step executed by the tool with both critics approving, or a named human attestation — see `AGENTS.md`.
