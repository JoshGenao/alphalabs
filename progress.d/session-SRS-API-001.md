=== SESSION SRS-API-001 ===
Date: 2026-06-30
Feature: SRS-API-001 — expose dashboard, CLI, and REST/WebSocket interfaces for operator workflows.
Outcome: serialized (passes:false) — operator-interface runtime substrate BUILT + integrated; domain handlers stay deferred.

OPERATOR DECISION (AskUserQuestion, session start): the SRS-API-001 contract layer
(API-2 REST / API-3 WS / API-4 CLI, all already passes:true) was shipped by S32;
the only remaining increment was a real operator-interface runtime. Operator chose:
(1) BUILD the runtime substrate, (2) KEEP it serialized (passes:false). This note
records that build.

What I did
----------
Built python/atp_runtime — the `operator-interface-runtime` named in
architecture/runtime_services.json#operator_workflow_surface_contract.deferred[].
Stdlib only (no web framework / no new dependency):
- registry.py — OperationKey / Request / HandlerResult / Handler + HandlerRegistry +
  DeferredHandler (inert structured 501 naming the owning feature) + invoke_handler
  (shared REST/CLI handler-failure serialisation: InterfaceError kept; TimeoutError ->
  504 GATEWAY_TIMEOUT; other -> 500 INTERNAL_ERROR; never a silent close).
- contract.py — per-capability/group/channel owner map, validated against the contract
  deferred[]; assert_closed_upstream_are_green (a closed-upstream owner must be
  passes:true); SHARED_GROUP_WORKFLOW_COMMANDS (scopes the shared `admin` CLI group so
  LOGS readiness only counts `admin logs`).
- handlers.py — runtime-owned SystemStatusHandler (ready=false while domain deferred;
  never overstates readiness), VersionHandler, ConfigHandler (secret-redacted schema).
- rest_server.py — RouteTable (404/405), transport-agnostic Dispatcher (route-level +
  action-level confirmation guard — lifecycle action=rollback requires confirm like the
  CLI), LoopbackHTTPServer (SRS-SEC-002: loopback + exact RFC1918 only; public/link-local
  fail closed), HTTP read timeout + body cap (413), real RFC6455 WebSocket upgrade with a
  bounded per-connection outbox + writer thread (slow consumer cannot block fan-out;
  deterministic writer shutdown; handler/dependency timeouts surfaced not swallowed).
- ws_frames.py / ws_protocol.py — RFC6455 codec + control-plane (SUBSCRIBE/ACK/EVENT
  {type,channel,data}/HEARTBEAT) + WsHub fan-out; deliver holds the session lock through
  emit so a publish cannot race an unsubscribe.
- cli_dispatch.py — routes the declared CLI commands through the same registry (atp_cli
  untouched so API-4 stays green); --confirm guard precedes dispatch; readiness-style
  commands exit NOT_READY when ready=false; status->exit-code mapping.
- runtime.py — OperatorInterfaceRuntime assembly + start/stop (not re-entrant — no
  listener leak)/publish/register_publisher/dispatch_rest; status_snapshot counts
  REST+CLI+WS operations (WS publisher obligations included; a workflow is fully_served
  only when EVERY operation is wired).
- __main__.py — the REAL operator CLI entrypoint `python -m atp_runtime` (atp_cli stays
  the declarative API-4 stub; it cannot dispatch through the runtime without a circular
  import). Advertised in discovery.
- tools/operator_interface_runtime_check.py — 13-collector contract evidence; wired into
  init.sh + ci.yml + run_ci_locally.sh (CI mirror, same slot).
- Prep commit: extended SAFETY_PATH_RE in tools/critic_check.py for the runtime paths.
- Contract reconciliation: the LOGS handler/CLI/WS owner is SRS-LOG-001 end-to-end
  (the actionable blocker; the runtime only provides the registry). Aligned all three
  artifacts — the owner map, both runtime_services.json blocks, and log_record_check's
  evidence string — so the LOGS attribution is consistent.

What I tested (per AC step)
---------------------------
Step 1 (./init.sh -> Environment ready): PASS — init.sh runs the new operator interface
  runtime check between the operator workflow surface check and the log record check and
  reaches "✓ Environment ready".
Step 2/3 (exercise the documented surfaces; verify the 8 AC workflows are available
  through documented API paths / CLI commands): PASS — demonstration: all 8 workflows
  (Live designation, Strategy management, Kill switch, Hot-Swap, Reservoir ranking,
  Backtests, System status, Logs) are reachable over a real loopback HTTP socket + real
  CLI dispatch. System status served (HTTP 200 / exit 0); the other 7 reachable-but-
  deferred (HTTP 501 / CLI 64) naming their owning features. Real WebSocket round-trip
  (handshake -> SUBSCRIBE/ACK -> publish/EVENT -> HEARTBEAT_PONG).
Step 4 (objective evidence; leave passes:false until end-to-end): DONE — passes stays
  false. End-to-end of the operator WORKFLOWS needs the domain handlers (deferred owners
  below) registered on the runtime registry. The interface SUBSTRATE is end-to-end
  demonstrable; the domain behaviour is each owner's AC.

Verification commands (all green on this branch):
- tools/run_ci_locally.sh step list: ruff check/format (clean repo-wide), cargo fmt
  --check (ok), cargo clippy --workspace -- -D warnings (clean), cargo test --workspace
  (ok), 16 architecture/contract checks (all ok incl. operator_interface_runtime).
- pytest -m "not integration and not e2e" -> 2502 passed, 4 pre-existing skips.
- package doctests (pytest --doctest-modules python/atp_runtime) -> 14 passed.
- tools/operator_interface_runtime_check.py -> PASS (13 collectors).

Critic verdicts
---------------
  deterministic (tools/critic_check.py): prep APPROVE (0); feat APPROVE (0);
    range origin/main..HEAD APPROVE (0). Safety-paired rule satisfied by
    tests/domain/test_operator_interface_runtime.py.
  judgment (tools/codex_review.sh origin/main, base=prep commit to scope out the
    critic_check.py prep edit per the documented refusal clause): 13 rounds run, EVERY
    needs-attention finding fixed before proceeding — (1) lifecycle action=rollback
    confirmation guard, (2) partial-readiness REST/CLI overstatement, (3) WS EVENT
    payload->data contract drift, (4) WS publisher obligations excluded from readiness,
    (5) WS publish/unsubscribe race, (6) start() re-entrancy listener leak, (7) unbounded
    body read + read timeout, (8) slow-WS-consumer blocks fan-out, (9) handle_error global
    TimeoutError suppression + CLI handler exception serialisation, (10) writer-thread
    leak on full outbox, (11) readiness false-ready exit code, (12) advertised CLI
    entrypoint ran the stub not the dispatcher, (13) SRS-MD-006 false-closed exemption +
    CLI internal/timeout exit-code mapping, (14) LOGS owner self-defer + admin-group
    readiness granularity. Round 15 (Codex): BLOCKED by Codex usage limit (resets 02:46).
  judgment fallback (Codex unavailable) per prompts/critic_prompt.md §Cross-environment:
    fresh-context sub-agent review of the feat diff -> WARN (1 non-blocking finding),
    then APPROVE-equivalent after the fix. The reviewer independently confirmed (against
    HEAD): dependency direction enforced + tested; WebSocket transport concurrency-safe
    (hub-then-session lock ordering, bounded per-connection outbox + held-and-joined
    writer thread, slow-consumer backpressure, publish-under-lock no-race L7 test);
    confirmation guard provably precedes dispatch on REST (route + action-level rollback)
    and CLI; loopback bind fails closed before any socket; handler/timeout failures
    surface as 504/500 not silent; no public-contract drift; owner map agrees with
    runtime_services.json. Its one WARN (contract.py code-comment inaccuracy: the
    _CLOSED_UPSTREAM_OWNERS rationale claimed those owners were NOT in deferred[] when
    they are) was FIXED by removing the now-redundant exemption entirely — every owner is
    now `runtime` or a feature named in the contract deferred[] (a stronger invariant),
    with a paired tests/domain/ status-owner-honesty test. Re-verified green after the fix.
  NOTE: the round-13 fix amend was initially (silently) blocked by the deterministic
    pre-commit safety-paired check (the round-13 delta touched atp_runtime safety paths
    with no tests/domain/ diff in that staged set); caught it, added the paired domain
    test, re-amended. Same for the WARN fix. Lesson: an amend that touches atp_runtime
    must include a tests/domain/ diff or the pre-commit hook blocks it.

Known issues / notes for next agent
-----------------------------------
- SRS-API-001 stays passes:false. The runtime SUBSTRATE is built + integrated; flipping
  to passes:true needs the DOMAIN handlers registered on atp_runtime.HandlerRegistry by
  their owners (named live at GET /api/v1/system/status and in the contract deferred[]):
  SRS-EXE-001 (kill switch / live designation), SRS-ORCH-004/005 (lifecycle / rollback),
  SRS-RESV-002/003 (ranking / Hot-Swap), SRS-BT-001/009 (backtests), SRS-DATA-002
  (watchlist), SRS-LOG-001 (logs), SRS-NOTIF-001 (alerts), SRS-MD-006 (domain readiness
  behind `readiness wait`), SRS-UI-001 (WS channel publishers / dashboard).
- A domain feature registers its handler with:
    runtime.registry.register(OperationKey(Surface.REST, "POST /api/v1/kill-switch"), handler)
  and a WS publisher with runtime.register_publisher("LOGS"). The status report flips
  fully_served per workflow as those land; SRS-API-001 flips passes:true when all 8
  required workflows are fully_served.
- The Codex judgment loop converged on the runtime's own robustness across 14 findings;
  the remaining work is NOT runtime defects but the deferred domain features above.
Resume / next: pick a deferred owner (e.g. SRS-LOG-001 to wire the logs handler, or
  SRS-EXE-001 for the kill switch) and register its handler on the runtime; or run the
  final Codex round after the usage limit resets to confirm the converged state.
