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
