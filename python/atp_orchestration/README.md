# atp_orchestration

Strategy-orchestration operator handlers — the `SRS-ORCH-005` rollback behaviour
behind the frozen SRS-API-001 CLI + REST contract.

- `mount_rollback(runtime, state_path=...)` composes onto an existing
  `atp_runtime.OperatorInterfaceRuntime` from above (the runtime never imports
  this package), registering the CLI `strategy rollback` command and the
  `action == "rollback"` leg of `POST /api/v1/strategies/{strategy_id}/lifecycle`.
  Every other lifecycle action keeps its honest structured 501 naming
  SRS-ORCH-004.
- The handler shells the cargo-built `orch005_rollback_cli`
  (`cargo build -p atp-orchestrator --bin orch005_rollback_cli`), which drives
  the fail-closed `StrategyOrchestrator::rollback` gate: SYS-80 previous-version
  retention, exact-target matching, and the NFR-S2 strategy-bound confirmation
  for a live rollback (the structural mirror of live promotion's control).
- Deferred owners (see `architecture/runtime_services.json`
  `rollback_contract.deferred[]`): the real live-designation probe
  (SRS-EXE-001 / SRS-RESV-*), the durable registry store, the dashboard rollback
  control (SRS-UI-001), and process-composition into a shipped main
  (SRS-API-001).

## `restart_schedule` — the SRS-MD-005 calendar half

`RestartSchedule` / `resolve_restart_instant_ns` turn the configured
`ATP_IB_RESTART_ET` (default `23:45`, US Eastern) into the epoch-nanosecond
instant the Rust restart-window classifier takes. It reuses
`atp_strategy.calendar.EASTERN` — a real `zoneinfo("America/New_York")` — so
23:45 ET is 03:45 UTC in EDT and 04:45 UTC in EST without any caller knowing
which.

The split is deliberate: `atp_types::RestartWindow` owns the SYS-75 phase
arithmetic and stays pure, and the calendar stays here. The Rust workspace has
no third-party crates and therefore no timezone database, so a Rust
implementation would hand-roll DST and fork an authority the repo already has —
and a missed adjustment would move the suspension window by an hour, suspending
trading at the wrong time while a real restart arrived unsuppressed.

Every malformed value raises `RestartScheduleError` rather than falling back to
the default. A window that looks configured and fires at the wrong hour is worse
than a startup failure the operator can read. `RestartSchedule.cli_args(date)`
hands the resolved instant and both durations to
`md005_connectivity_restart_window_cli` together, so a caller cannot pass the
instant while silently keeping default durations.

