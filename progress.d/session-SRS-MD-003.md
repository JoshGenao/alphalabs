=== SESSION SRS-MD-003 ===
Date: 2026-07-16
Feature: SRS-MD-003 — monitor market data and broker heartbeat freshness continuously (SyRS SYS-39, NFR-P5 ≤15 000 ms; StRS SN-2.03). AC: staleness over 15 seconds is detected, logged, displayed, and reflected in system health status.
Outcome: serialized (code integrated; passes stays false — the LIVE feed-loop producer is deferred; see Codex R8 and Resume/next)

What I did:
- `HeartbeatFreshnessMonitor` in crates/atp-market-data — the TIME-based companion to SRS-MD-007's `SequenceGapDetector`, mirroring its idioms: injected epoch-ns clock (no wall-clock I/O), canonical `SecurityKey` keying, fail-closed never-observed ⇒ `Stale` with `staleness_ms: None` (no fabricated age), transition state committed BEFORE fallible `HeartbeatEventSink` publication, one `HeartbeatStalenessEvent` per Fresh↔Stale flip (never per evaluation). Boundary is strictly OVER 15 s at ns precision (`heartbeat_age_ns_is_stale`: age_ns > 15 000·10⁶; exactly 15.000 s is Fresh). `combined_line_freshness()` = gap-stale OR time-stale — the merged per-line view `sequence_gap_contract.deferred[]` assigned to MD-003.
- Vocabulary in atp-types: `HeartbeatFeed` (MarketData{symbol,asset_class} | Broker — vendor-neutral), `HeartbeatTransition`, `HeartbeatStalenessEvent` (threshold snapshotted; no broker/vendor/session/tick-id fields). Reused the existing NFR-P5 authority `HEARTBEAT_STALENESS_THRESHOLD_MS` (perf.rs) — no new constant.
- `md003_heartbeat_cli` (fixture operator binary): replays a directive script (watch-security/watch-broker/tick/broker-heartbeat/resync/evaluate), feeds monitor + SequenceGapDetector, emits kv status rows (time_stale/gap_stale/merged stale) + transition events; fail-closed parser (exit 2 + structured stderr).
- Python bridge python/atp_dashboard/heartbeat.py: `CliHeartbeatSource` (bounded subprocess, injectable runner, appends `evaluate <time.time_ns()>` — the single wall-clock read at the outermost edge; rejects foreign evaluation instants) + `HeartbeatFreshnessProvider` (HEARTBEAT channel rows with the declared payload fields; /dashboard/api/heartbeat snapshot; health_summary; once-per-flip HEARTBEAT_STALE/RECOVERED LogRecords under Source.MARKET_DATA / Source.IB_GATEWAY with cross-poll baseline + failed-write retry — a fresh CLI process per poll would otherwise re-log every second).
- Wiring: DashboardPublisher gets a `heartbeat` provider on its OWN isolated 1 s ticker (inventory precedent — subprocess must not starve the main tick; HEARTBEAT leaves the main ticker when mounted, no double publish); mount_dashboard/mount_default_dashboard (env ATP_MD003_OBSERVATIONS + optional ATP_MD003_LOG_DIR); ReadinessBackedProvider.system_snapshot().health.market_data_heartbeat (real when mounted, honest deferred cell otherwise); SPA per-feed keyed heartbeat rows (fresh/stale pill; "no data" for never-observed).
- Taxonomy: HEARTBEAT_STALE/HEARTBEAT_RECOVERED added under MARKET_DATA + IB_GATEWAY in records.py + log_record_contract (runtime_services.json) + atp_logging README, in lockstep (dict-equality anchors verified).
- Contract: new `heartbeat_freshness_contract` block with deferred[] pinning the live feed loop + reqCurrentTime producer obligation (IbGatewayConnection UNCHANGED — live-verified/golden-pinned), the MD-004 orchestrator probe bridge (owned by MD-004), MD-005 restart-window suppression, MD-006/NOTIF-001 integration.
- SAFETY_PATH_RE prep commit DROPPED before integrate: Codex round 1 blocked it (meta:critic-self-modification — the judgment layer refuses any tools/critic_check.py change without human review; same lesson as EXE-009). The tests/domain/ pairing ships anyway (voluntary, the MD-007 precedent). OPERATOR ACTION: if heartbeat paths should be critic-enforced, apply this one-liner under human review — extend SAFETY_PATH_RE with `|/heartbeat\.py|heartbeat[_-]?freshness|heartbeat[_-]?staleness|md003[_-]?heartbeat|srs[_-]?md[_-]?003` before the closing paren.

What I tested (per step):
  Step 1: PASS — ./init.sh → "✓ Environment ready".
  Step 2: PASS — fixture CLI workflow: obs script (tick AAPL + broker-heartbeat @T0) → `md003_heartbeat_cli`: `evaluate T0+15.000s` → all rows stale=false (boundary Fresh); `evaluate T0+15.000s+1ns` → stale=true + 2× `event kind=HEARTBEAT_STALE`; fresh tick+heartbeat → 2× `HEARTBEAT_RECOVERED`. Gap fixture (seq 1→5) → `SEQUENCE_GAP` + gap_stale=true/stale=true while time_stale=false (merge proven). Malformed directives → exit 2.
  Step 3: PASS — detected (above); displayed: in-process runtime + mount_dashboard(heartbeat=…) → publish_once() delivers one HEARTBEAT event per feed (market_data:AAPL, ib_gateway) with staleness_seconds/is_stale; REST GET /dashboard/api/heartbeat → any_stale=true; reflected in health: GET /dashboard/api/system → health.market_data_heartbeat = {state:STALE, any_stale:true, stale_feeds:[ib_gateway, market_data:AAPL]}; logged: JsonlLogStore segment holds WARN HEARTBEAT_STALE records (both sources) once per flip across repeated polls, INFO HEARTBEAT_RECOVERED on recovery (persisted output inspected).
  Step 4: PASS (as serialized) — "leave passes false until the evidence proves the requirement end to end": passes STAYS FALSE. The fixture/mock evidence above proves detection/logging/display/health mechanics end to end through the real monitor, CLI, publisher, REST, and log store; what it cannot prove is the LIVE producer (real IB ticks + genuine gateway keepalives), which is the pinned deferred[] runtime — hence serialized, per Codex R8's named remedy. Verification run on the final tree: cargo test --workspace 1700 passed / 0 failed (incl. 13-test srs_md_003_heartbeat_freshness + in-lib boundary tests + CLI process tests); pytest -m "not integration and not e2e" 3726 passed (incl. 20+ unit + 2 boundary + 5 contract + 8 domain new tests); mypy/cargo fmt/ruff check+format/clippy clean on all touched files; all 22 tools/*_check.py contract checks pass. NOTE: repo-wide `ruff format --check .` and mypy baselines are red on UNTOUCHED files — pre-existing drift on origin/main (see project memory "CI red behind format gates"), not introduced here.

Critic verdicts:
  deterministic: APPROVE — no findings (staged + --range origin/main..HEAD)
  judgment (codex_review.sh): APPROVE (round 5) — "No material, line-citable blocker found in the diff against origin/main." Convergence history, every finding fixed + regression-tested in the same commit:
    R1 needs-attention: meta:critic-self-modification (the SAFETY_PATH_RE prep commit touches tools/critic_check.py — the judgment layer refuses critic self-modification) → prep commit DROPPED; operator one-liner recorded above.
    R2 needs-attention: (a) empty watch set reported FRESH (watched_feeds:0 masquerading as healthy) → observe() now refuses zero-row scripts AND scripts that never watch the broker feed (SYS-39 requires it); (b) one feed's failed audit write masked by another's success in the same poll → log_write_ok now aggregates per poll.
    R3 needs-attention: (a) out-of-order poll completions (WS ticker vs REST vs health, observe outside the lock) could commit an OLDER evaluation late and fabricate transitions → monotonic-evaluation guard (only strictly-newer evaluated_at_ns advances the baseline; older polls are display-only); (b) 5 s subprocess budget exceeded the 1 s channel cadence → default budget now 0.9 s (below cadence, contract-tested).
    R4 needs-attention: log_store=None treated as write-success — a mounted monitor could drop the mandatory audit trail by configuration → log sink now REQUIRED (provider refuses None; mount_default_dashboard fails closed at boot when ATP_MD003_OBSERVATIONS is set without ATP_MD003_LOG_DIR).
    R5: approve (pre-rebase tree).
    [rebase onto latest origin/main — SRS-RES-001 landed; additive conflict resolution in server.py (research + heartbeat providers coexist); full gates re-run green]
    R6 needs-attention: a transient sink failure during a brief stale incident could lose BOTH the incident and its recovery (baseline-holdback skipped fresh==fresh next poll) → pending-record QUEUE: the flip baseline advances on the fact of the flip, failed records queue and retry oldest-first every poll (chronological audit stream), log_write_ok = queue-empty, queue bounded with dropped_log_records surfaced; regression + bound tests added.
    R7 needs-attention: (a) a slower OLDER evaluation finishing late was still SERVED to display/health (an older fresh snapshot could overwrite a newer committed stale one) → late callers now receive the cached committed observation; (b) gap-only staleness (time_stale=false, gap_stale=true) was logged as HEARTBEAT_STALE, misfiling an SRS-MD-007 SEQUENCE_GAP incident → HEARTBEAT_* records now classify TIME staleness only (row time_stale); the merged verdict stays the display/health surface. Both regression-tested.
    R8 needs-attention: (a) the LIVE producer (async feed loop calling observe_tick per real IB tick + observe_broker_heartbeat per genuine reqCurrentTime/keepalive) is deferred, so production would replay fixture freshness — Codex's named remedy: "integrate this as serialized/incomplete with the feature remaining false" → ACCEPTED; outcome reclassified serialized (a live-IB session also cannot run in a parallel worktree — single-live invariant). (b) a broker-only observation script passed as healthy market-data monitoring → symmetric market-data-required guard added (+ test); the deferred live loop owns the legitimate zero-active-subscriptions state.
    R9 needs-attention (final, recorded verbatim): sole residual = "the change still defers the real live feed loop"; its own recommendation: "Wire the monitor to the runtime market-data subscription manager and IB Gateway heartbeat/keepalive source, or land this as a serialized skeleton without claiming SRS-MD-003 is implemented/shippable." → the SECOND remedy is exactly this integrate: --mode serialized, passes stays false, no completeness claim. All in-scope findings (R2-R8: 8 distinct defects) were fixed and regression-tested before landing; the residual is the pinned deferred[] live producer — the documented non-convergence-on-deferred-runtime-slices case (a live-IB feed loop cannot be built or verified in a parallel worktree; single-live invariant).

Resume / next — WHAT FLIPS THIS COMPLETE: the live feed loop (async runtime over the EXE-006 live-verified IB wire) that (a) calls monitor.observe_tick per genuinely delivered IB tick for each consolidated subscription, (b) calls monitor.observe_broker_heartbeat per genuine gateway round-trip (reqCurrentTime/keepalive response — NOT local liveness), (c) runs evaluate on a periodic cadence, and (d) feeds the same HEARTBEAT channel / health / log path built here (swap CliHeartbeatSource for the live source — everything downstream is source-agnostic). Requires a live IB Gateway session (single-live invariant → an exclusive session or operator run), then verify staleness detection by pausing the feed >15 s and confirming detected/logged/displayed/health, then flip via verified-e2e or close_feature. Everything else is built: do NOT rebuild the monitor/CLI/provider/wiring. Other deferred[] owners: MD-004 orchestrator probe bridge (use combined_line_freshness), MD-005 suppression, MD-006 readiness + NOTIF-001 alert routing. Dropped SAFETY_PATH_RE prep (critic self-modification — needs human review): see operator one-liner above.

---

=== SESSION SRS-MD-003 (2) — the live producer, 2026-07-31 ===
Session-2 outcome: serialized (passes STAYS false). The first `Outcome:` line above is unchanged
on purpose — `serialized_notes()` reads only that one.
Branch: agent/SRS-MD-003-stream (a fresh worktree; the stale alphalabs-wt-SRS-MD-003 was left alone).

WHAT THIS SESSION BUILT — the deferred live feed loop Session 1 named. Do NOT rebuild it.

1. IB INBOUND STREAMING SURFACE (the gap that blocked this feature, and MD-007/SDK-004/SAFE-002):
   `wire.rs` had 7 request/response ops and no tick delivery at all — `subscribe_market_data` sends
   `reqMktData`, waits for the confirm frame, and STOPS READING. Added:
   * `IbSession::current_time_round_trip()` — reqCurrentTime(49) -> currentTime(49). The REPLY is
     the only evidence accepted: a half-open TCP socket stays writable long after the peer is gone,
     so a successful send proves only that WE are alive.
   * `IbSession::poll_market_data(active_tickers, budget)` — drains tickPrice(1)/tickSize(2) into
     `DeliveredTick { ticker_id, tick_type }`. Budget expiry returns the ticks collected so far
     (a quiet line is NOT an error — quiet is the exact condition the monitor judges); a
     non-informational error naming one of OUR tickers (e.g. 10197 withheld stream) fails closed.
   * Two thin `TcpIbGateway` methods, deliberately NOT on `IbGatewayConnection`: that trait is the
     request/reply broker contract every implementor must serve, and widening it would force a
     heartbeat implementation onto fixtures with no wire to heartbeat over.

2. THE LOOP — `crates/atp-market-data/src/live_feed.rs`: `LiveTickSource` port + `LiveFeedLoop`.
   Observes per delivered tick, round-trips the broker on cadence, evaluates, writes a durable
   snapshot. Three properties carry it: only evidence observes (a FAILED round trip records
   nothing, so the broker line ages honestly); a step ALWAYS evaluates even when the source errored
   (bailing early freezes the snapshot, and a frozen snapshot looks exactly like a healthy one);
   the snapshot is self-dating. `new()` refuses cadence >= 15 s threshold, poll budget >= cadence,
   and an empty watch set.

3. NO FABRICATED tick_seq — `HeartbeatFreshnessMonitor::observe_security(key, at_ns)` added.
   The IB tick stream carries NO upstream sequence number, and `MarketDataTick::tick_seq`'s producer
   contract says a re-numbered delivery counter there "would be gap-free by construction and would
   silently defeat gap detection". So the live loop never constructs a tick.
   ** OPERATOR-VISIBLE FINDING: SRS-MD-007's live gap detection is not satisfiable from this
   source at all. ** TWS exposes no sequenced market-data stream; rendered rows carry
   `gap_stale=false` meaning "no gap detection is RUNNING", never "a detector cleared this line"
   (recorded in heartbeat_freshness_contract.live_feed.gap_detection). MD-007's live leg needs a
   sequenced source that this vendor API does not provide — that is a scoping decision, not a bug.

4. COMPOSITION — `crates/atp-orchestrator/src/live_market_data.rs` (`IbLiveTickSource`) +
   `md003_live_feed_cli`. atp-adapters and atp-market-data must not see each other, so the
   orchestrator (the composition layer, exempt from the dependency allow-list) binds them; only a
   Cargo.toml dep was needed, no prep commit. The feed opens its OWN IB client id: sharing the
   execution session would let a tick arriving mid-`submit_order` be eaten by that op's frame loop,
   so a line would go quiet for reasons unrelated to the market. Allow-list arg parser; refuses
   unknown flags, zero/negative numerics, cadence >= threshold, poll budget >= cadence.

5. DASHBOARD — `SnapshotHeartbeatSource` beside `CliHeartbeatSource` in atp_dashboard/heartbeat.py,
   same Protocol, same kv grammar (extracted `_status_row`/`_event_row` so ONE parser serves both).
   Everything downstream is untouched, as Session 1 promised. Fail-closed: a snapshot older than
   `max_age_ms` is refused -> the provider's honest UNAVAILABLE / any_stale:true. `data_source` now
   reports the producer that actually answered instead of a hardcoded fixture name.

6. SRS-DATA-015 — registered `md003-heartbeat-snapshot` (magic + version, Pinned, never legacy);
   entity_count 15 -> 16.

WHAT I TESTED
  cargo test --workspace: 2027 passed / 0 failed.
  L4 (new, own harness) crates/atp-adapters/tests/srs_md_003_ib_stream.rs: 8 passed — the EXE-006
    wire test is digest-pinned, so appending there would have invalidated live evidence for reasons
    unrelated to the wire it proves.
  L1 live_feed unit tests: 9 passed. atp-orchestrator (ib-live-transport): CLI arg + ticker-id: 8.
  L7 tests/domain/test_live_heartbeat_feed.py: 8 passed — dead-daemon snapshot never reads healthy,
    foreign/unknown-version files refused whole, broker-only rows rejected, and the Rust
    failed-round-trip invariant shelled directly.
  pytest -m "not integration and not e2e": 4408 passed, 8 FAILED — ALL EIGHT are the EXE-006/EXE-007
    evidence-digest tripwire firing because the two pinned wire files changed. That is the tripwire
    doing its job, and it is the operator's authorized cost for this change; no other regression.
  Only my 3 new files were rustfmt'd (never a whole-crate fmt — it rewraps literal contract anchors).
  clippy clean on atp-adapters + atp-market-data. PRE-EXISTING and untouched: clippy fails on
  crates/atp-orchestrator/tests/resv_3_trigger_log_schema.rs ("doc list item overindented") — that
  doc comment is byte-identical on origin/main; it belongs to the toolchain-pin owner, not here.

RESUME / NEXT — the operator live window, in order:
  1. `ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py` — regenerates
     architecture/ib_paper_account_evidence.json. THE BRANCH CANNOT GO GREEN WITHOUT THIS: the 8
     failures above are exactly this gate, by design.
  2. `cargo build -p atp-orchestrator --features ib-live-transport --bin md003_live_feed_cli`
     then `md003_live_feed_cli --snapshot <path> --symbol AAPL --client-id <dedicated>`.
     Point the dashboard at it: `SnapshotHeartbeatSource(<path>)` in place of `CliHeartbeatSource`.
  3. Confirm ticks arriving + health FRESH; then PAUSE the feed > 15 s and confirm all four AC legs:
     detected, HEARTBEAT_STALE logged, dashboard row stale, health.market_data_heartbeat flipped.
     Resume -> HEARTBEAT_RECOVERED. Then `close_feature.py SRS-MD-003 --verified`.
  Not done here and not claimed: MD-004's probe bridge, MD-005 suppression, MD-006/NOTIF-001 routing
  (unchanged deferred owners), and mounting the snapshot source in mount_default_dashboard behind an
  env knob — deliberately left to the operator so no unattended dashboard silently swaps producers.

--- JUDGMENT-CRITIC ROUNDS (2026-08-01, reviewer=codex) ---
Four rounds. Every in-scope finding was fixed AND regression-tested in the same commit; the one
architectural residual is scoped to its owner below. No finding was waved through.

R1 [high] the heartbeat round trip consumed interleaved tick frames. `current_time_round_trip`
  loops to `currentTime` and dropped every other frame; the feed's poll and heartbeat share one
  session, so ticks arriving mid-round-trip were destroyed — a LIVE line could age into a false
  staleness alarm. FIXED: `IbSession::pending_ticks` buffers them (bounded, oldest-dropped) and
  `poll_market_data` drains it first. Test: ticks_arriving_during_a_heartbeat_survive_to_the_next_poll.
R2 [high] transitions were lost between dashboard polls. The provider derives records from SAMPLED
  rows, and the daemon evaluates far more often than the dashboard polls, so a stale->recover cycle
  between two polls left every sampled row fresh and erased a real incident from the audit trail.
  FIXED: the snapshot now carries a bounded transition JOURNAL (64), each entry logged exactly once
  (dedup on kind+feed+evaluated_at_ns); transitions at the current instant still come from the rows,
  so the fixture-CLI path is untouched. Tests: a_stale_recover_cycle_between_polls_is_still_logged,
  a_journalled_transition_is_logged_exactly_once.
R2 [high] future-dated snapshots bypassed the dead-daemon guard — a negative age is trivially "not
  older than the limit", so a file stamped ahead of the clock pinned a fresh verdict AND poisoned the
  provider's monotonic guard (every genuine snapshot after it reads as late). FIXED: fail closed
  beyond a 1 s tolerance. Test: test_a_future_dated_snapshot_is_refused_not_treated_as_current.
R3 [high] a mid-frame budget expiry desynchronized the stream. `read_exact_deadline` accumulates into
  a LOCAL buffer, so a deadline that lands partway through a frame loses the bytes already taken off
  the socket and the remainder is parsed as a new frame. Pre-existing, but MY short poll budget
  (500 ms, re-armed continuously) is what makes it reachable. FIXED: read_frame now reports whether
  the expiry was mid-frame; only a clean between-frames expiry is an empty poll, a mid-frame one is a
  transport fault so the session is dropped and rebuilt. All 29 EXE-006 wire tests still pass.
  Test: a_frame_split_across_the_budget_fails_closed_instead_of_desyncing.
R3 [medium] the tick that CONFIRMS reqMktData was discarded, so an illiquid line could read
  never-observed while IB had already delivered data. FIXED: buffered before returning the receipt.
  Test: the_tick_that_confirms_a_subscription_is_not_lost.
R4 [high] subscriptions were lost after a transport reconnect — and R3's fix is exactly what makes a
  reconnect likely. TcpIbGateway rebuilds a dropped session transparently, but nothing replayed
  reqMktData, so the feed polled dead ticker ids forever: a permanent, false staleness alarm.
  FIXED: TcpIbGateway::session_generation() + IbLiveTickSource re-subscribes before polling whenever
  the generation moved. Test: the_session_generation_advances_on_every_reconnect.
R4 [high] SUBSCRIPTION OWNERSHIP — NOT FIXED, SCOPED (operator authorization requested). Codex wants
  the monitor fed by the Market Data Subscription Manager's consolidated stream instead of its own
  reqMktData lines. Correct in principle and NOT buildable now: SRS-MD-001 is passes:false and
  `MarketDataSubscriptionManager` is an empty struct — there is no consolidated stream to observe.
  Recorded in heartbeat_freshness_contract.live_feed.subscription_ownership with the two concrete
  consequences (freshness could read healthy while a different strategy path is wedged; these lines
  are spent outside the manager's dedup/line-limit accounting) and what MD-001 must do to close it.
  This is the documented deferred-slice non-convergence case: fix the in-scope bugs, scope the rest
  to a named owner, never fake an APPROVE.
