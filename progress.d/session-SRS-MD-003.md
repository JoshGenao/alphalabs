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

=== SESSION SRS-MD-003 (3) — the mount + three adversarial rounds, 2026-08-03 ===
Session-3 outcome: NOT INTEGRATED (branch parked, lease released). The first `Outcome:`
line at the top of this file is unchanged on purpose — `serialized_notes()` reads only that one.
Branch: agent/SRS-MD-003-stream, rebased onto origin/main (was 17 behind), 5 commits ahead.

THE ONE THING BLOCKING THIS FEATURE — read this first.
  The IB Gateway never became reachable this session, so the operator live window never ran.
  It is UP (systemd ibgateway.service active via IBC, VM reachable on ssh) but WEDGED: IBC
  logged in at 03:59:24 and stopped at a modal dialog —
      "detected dialog entitled: Existing session detected"
      "User must choose whether to continue with this session (scenario 1)"
  because /home/jgenao622/ibc/config.ini sets ExistingSessionDetectedAction=manual and nobody
  clicked. No API port is bound (ss shows nothing on 4001/4002); polled every 15 s for 45 min.
  REMEDY (operator choice): either click through it on the VM console/VNC, or set
  ExistingSessionDetectedAction=primary and restart ibgateway.service so IBC dismisses it.
  ALSO CHECK BEFORE THE WINDOW: config.ini has AcceptIncomingConnectionAction=manual and
  jts.ini has TrustedIPs=127.0.0.1 — a connection from a non-local IP will pop an accept
  dialog and hang. Add the client IP to the trusted list or accept it once at the console.

  Consequence: the branch CANNOT INTEGRATE IN EITHER MODE right now. Session 2's wire change
  invalidated the SRS-EXE-006/EXE-007 evidence code_digest, so 8 tests are red AND `./init.sh`
  itself fails ("IB adapter runtime check failed"). Only
  `ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py` against the live paper account
  clears it. Landing without it would push a red main.

WHAT THIS SESSION BUILT
1. THE MOUNT — the last unwired seam. `mount_default_dashboard` only ever read
   ATP_MD003_OBSERVATIONS and always built the fixture CliHeartbeatSource, so the "displayed"
   and "reflected in system health" AC legs could not be demonstrated against the LIVE producer
   at all. Added ATP_MD003_SNAPSHOT (python/atp_dashboard/server.py). Exactly one producer may
   be configured: setting both fails closed at boot (two producers on one channel leaves it
   ambiguous which one health reflects, and a fixture's verdicts read as live evidence is an
   attribution bug on a safety surface). ATP_MD003_LOG_DIR stays mandatory for either.
2. `HeartbeatSource` Protocol (python/atp_dashboard/heartbeat.py) — the provider now depends on
   the structural contract both sources already satisfied, not the concrete fixture class.
3. Rebased onto origin/main. One conflict, crates/atp-adapters/src/lib.rs: my `DeliveredTick`
   re-export met SRS-NOTIF-001's `pub use notification::{...}`. Additive — kept both.

THREE JUDGMENT-CRITIC ROUNDS (reviewer=codex each time). Four real defects, all fixed and
MUTATION-VERIFIED (each regression test was confirmed to FAIL with the fix reverted):
R5 [high] Partial resubscribe leaked duplicate IB lines. `subscribe_all` only committed
  `self.lines` after the whole loop succeeded, so a late failure left the earlier successful
  reqMktData open on the wire but UNTRACKED and left `subscribed_generation` stale — and
  `resubscribe_if_reconnected` then fired on every single step, opening a fresh duplicate line
  each time until IB's line allowance was gone. Now the lines that opened are always committed
  with their generation; the failed symbol ages into STALE (the truth about a line carrying no
  data) and a genuine reconnect still retries everything.
  Test: a_partial_subscribe_commits_the_lines_that_opened (drives a scripted fake IB server).
R6 [high] A second subscribe ate a live line's ticks. One socket carries every line, so while
  `subscribe_market_data` waited for symbol #2's confirmation, ticks for the already-subscribed
  symbol #1 fell through the match arm and were DISCARDED — aging a line that was actively
  flowing into a false staleness alarm, on every multi-symbol connect and every resubscribe.
  Same shared-socket hazard session 2 fixed for `current_time_round_trip`, never fixed on the
  subscribe path. Every decodable tick is now buffered; the ticker id decides only CONFIRMATION.
  Test: ticks_for_another_line_survive_a_second_subscribe.
R7 [high] The verdict was dated BEFORE the blocking probe. The CLI sampled the clock and passed
  the reading into `step`, but a step blocks — the drain spends its poll budget and the broker
  probe can sit on the wire to its operation deadline. So a broker whose last answer landed near
  the threshold could be judged FRESH after the real clock had already crossed 15 s, keeping the
  dashboard green through exactly the window this feature exists to catch. `step` now takes the
  CLOCK and reads it twice: observations keep the pre-I/O reading (dating an observation early
  only ages a line faster — fail-closed), the EVALUATION uses a post-I/O reading floored at the
  first. FeedStep carries `evaluated_at_ns` and the CLI stamps the snapshot with it, so the
  header instant and the row verdicts can never disagree.
  Tests: a_broker_timeout_that_crosses_the_threshold_is_stale_immediately (L1) +
  test_a_broker_timeout_crossing_the_threshold_publishes_stale_at_once (L7 shell).

THE RESIDUAL — NOT FIXED, SCOPED TO ITS OWNER (unchanged from session 2, re-raised in R5 and R7)
  SUBSCRIPTION OWNERSHIP. Codex wants freshness sourced from the Market Data Subscription
  Manager's consolidated stream instead of the daemon's own reqMktData lines. Still not
  buildable: SRS-MD-001 is passes:false and `MarketDataSubscriptionManager` is an empty struct —
  there is no consolidated stream to observe. What THIS session changed is exposure (the new
  mount makes the producer production-reachable), so the mitigation is honesty at the point of
  choice: the scope limit ("a FRESH verdict means those lines are delivering, NOT that every
  path a strategy consumes is healthy") is stated at the mount point, the knob is opt-in and
  unset by default, and heartbeat_freshness_contract.live_feed.subscription_ownership records
  the production exposure alongside the original consequences and MD-001 as owner.
  Codex's own second remedy is "keep this mount non-production and non-shippable until MD-001
  owns the same subscriptions" — which is what passes:false plus an opt-in, default-off knob
  means. THIS NEEDS OPERATOR AUTHORIZATION before integrating, exactly as session 2's R4 did.
  The documented deferred-slice non-convergence case: fix every in-scope bug, scope the rest to
  a named owner, never fake an APPROVE.

WHAT I TESTED (per feature step)
  Step 1: FAIL — `./init.sh` → "✗ Environment failed / IB adapter runtime check failed".
    Single cause: the stale evidence digest. Everything before that step passed. This is the
    tripwire doing its job, not a broken environment.
  Step 2: PASS (fixture/mock legs) — 4541 passed in pytest -m "not integration and not e2e";
    the new mount serves the live snapshot through the real runtime (L4
    test_default_mount_serves_the_live_snapshot_producer: GET /dashboard/api/heartbeat
    any_stale=true, GET /dashboard/api/system health.market_data_heartbeat STALE with
    data_source=md003_live_feed_cli). NOT PASS on the live leg: no IB session was reachable.
  Step 3: PARTIAL — detected/logged/displayed/health all proven end to end through the real
    monitor, provider, publisher, REST and log store, but over a SNAPSHOT FIXTURE, not real IB
    ticks. The genuine Fresh->Stale transition on a live market-data line remains unwitnessed.
  Step 4: passes STAYS FALSE. The evidence does not prove the requirement end to end.
  Gates: cargo test --workspace 0 failed. pytest 4541 passed / 8 failed (the digest tripwire,
    all 8). 20 of 22 tools/*_check.py pass; the 2 failures are that same tripwire.
    clippy + ruff clean on every touched file; mypy python/ at its pre-existing baseline
    (4 errors, all in the untouched atp_orchestration/hot_swap_triggers.py).
    NOTE one flake seen once and not reproduced: tests/test_concurrent_read_contract.py::
    AggregateEvidenceTest::test_run_checks_emits_ten_items failed in a run that immediately
    followed a cargo test --workspace (its cargo smoke contends on the target lock); it passed
    alone and in the next two full runs. Not caused by this branch.

Critic verdicts:
  deterministic: APPROVE — no findings (run before each of the 3 commits)
  judgment (adversarial_review.py, reviewer=codex): rounds 5, 6, 7. R5 and R6 blocked on
    defects that are now fixed + regression-tested. R7 blocks SOLELY on the MD-001 subscription
    ownership residual above — no in-scope defect remains. NOT an APPROVE, and not recorded as
    one.

RESUME / NEXT — in order:
  1. Unwedge IB Gateway (see the top of this section). Confirm `ss -ltn` shows 4002 bound.
  2. `ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py` — regenerates
     architecture/ib_paper_account_evidence.json. This is what clears the 8 red tests AND
     `./init.sh`. Read the PER-OP output, not just the exit code (the MD-006 false-green lesson:
     a harness once reported PASSED on 0/6 successful IB ops).
  3. `cargo build -p atp-orchestrator --features ib-live-transport --bin md003_live_feed_cli`,
     then run it with a DEDICATED --client-id and --snapshot <path>. Point the dashboard at it
     with ATP_MD003_SNAPSHOT=<path> ATP_MD003_LOG_DIR=<dir> (new this session — no code needed).
  4. US equities must be OPEN: the wire encodes STK/SMART/USD only, and reqMktData asks for
     delayed data (type 3, no entitlement needed) which still requires market hours. Confirm
     ticks arriving and health FRESH, then PAUSE the feed >15 s and confirm all four legs:
     detected, HEARTBEAT_STALE logged, dashboard row stale, health.market_data_heartbeat
     flipped. Resume -> HEARTBEAT_RECOVERED.
  5. Get operator authorization on the subscription-ownership residual, then
     `integrate --mode complete` (or close_feature.py SRS-MD-003 --verified).
  Do NOT rebuild the monitor, fixture CLI, provider, live feed loop, IbLiveTickSource, or the
  dashboard mount — all built and adversarially converged. Unchanged deferred owners: MD-004
  probe bridge, MD-005 suppression, MD-006/NOTIF-001 routing, MD-007 live gap detection
  (unsatisfiable from this vendor API — TWS exposes no sequenced market-data stream).

--- SESSION 3, PART 2: THE OPERATOR LIVE WINDOW ACTUALLY RAN (2026-08-03 11:33–12:00 UTC) ---

THE GATEWAY BLOCKER, AND THE SECOND ONE BEHIND IT.
  The operator cleared the "Existing session detected" modal and IBC completed login
  (account DU5302722, paper, Read-Only API false). Port 4002 bound. But every request still
  died: handshake OK, then `Connection reset by peer` / `closed the connection mid-frame`.
  CAUSE: the gateway's jts.ini carries `TrustedIPs=127.0.0.1` (regenerated on today's boot),
  and this Mac is on a different subnet (192.168.2.x vs the VM's 10.0.0.x), so the gateway
  accepted the socket and then dropped an untrusted API client.
  FIX USED — an SSH port-forward, so the gateway sees a trusted 127.0.0.1, and NO change was
  made to the trading VM's configuration:
      ssh -N -L 14002:127.0.0.1:4002 <vm>
      ATP_IB_HOST=127.0.0.1 ATP_IB_PAPER_PORT=14002 <command>
  Use this for every future live window from a non-VM host; it is cheaper and safer than
  editing TrustedIPs or setting AcceptIncomingConnectionAction=accept on a trading gateway.

EVIDENCE REGENERATED — the tripwire is cleared.
  `ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py` -> SRS-EXE-006 PASS, operator
  paper-account round trip GREEN, result_line "test result: ok. 1 passed" (asserted, not a
  MD-006-style non-asserting diagnostic — the per-op output was read).
  `python3 tools/ib_api_version_check.py --sync` -> SRS-EXE-007 PASS.
  Run TWICE: the Mutex cfg fix touches interactive_brokers.rs, one of the three digest files.
  ./init.sh now reaches "✓ Environment ready"; pytest is 4549 passed / 0 FAILED.

A REAL CI REGRESSION THIS BRANCH INTRODUCED, found only by running the gate.
  Session 2 inserted `use std::sync::atomic::{AtomicU64, Ordering};` directly above
  `use std::sync::Mutex;`, and the `#[cfg(feature = "ib-live-transport")]` that had guarded
  Mutex ended up guarding AtomicU64 — leaving Mutex unconditionally imported. Every build we
  had run enabled the feature, so nothing caught it; `cargo clippy --workspace -- -D warnings`
  (default features) did. Gate restored and now PINNED by
  tests/domain/test_ib_adapter_envelope.py::test_the_default_build_carries_nothing_from_the_live_transport
  (mutation-verified). The rule was already written in that file's docstring; it just had no test.

ALL FOUR AC LEGS, PROVEN LIVE — `md003_live_feed_cli` against the real gateway, AAPL,
dedicated client id, delayed data (reqMarketDataType 3). Both feeds show never_observed=false,
i.e. genuine IB tick deliveries and genuine reqCurrentTime round trips.
  FRESH  -> GET /dashboard/api/heartbeat 200 ok:true any_stale:false
            market_data:AAPL staleness_ms=10819 stale=False; ib_gateway staleness_ms=3057
            health.market_data_heartbeat {state: FRESH, data_source: "md003_live_feed_cli"}
  CUT the feed (daemon left running) ->
  DETECTED: event kind=HEARTBEAT_STALE feed=broker      staleness_ms=15001
            event kind=HEARTBEAT_STALE feed=market_data staleness_ms=15000
            (the market-data event is 15000.223 ms at ns precision — strictly OVER the
            threshold, truncated to 15000 in the ms field. The boundary behaves exactly as
            specified: over, not at.)
  DISPLAYED: GET /dashboard/api/heartbeat 200 any_stale:true
            market_data:AAPL staleness_ms=45326 stale=True; ib_gateway staleness_ms=37565 stale=True
  HEALTH:   health.market_data_heartbeat = {"ok": false, "state": "STALE", "any_stale": true,
            "stale_feeds": ["ib_gateway", "market_data:AAPL"], "watched_feeds": 2,
            "threshold_ms": 15000, "data_source": "md003_live_feed_cli"}
  LOGGED:   persisted JsonlLogStore records — WARN HEARTBEAT_STALE source=MARKET_DATA and
            WARN HEARTBEAT_STALE source=IB_GATEWAY.
  RESTORE -> HEARTBEAT_RECOVERED on both feeds; INFO HEARTBEAT_RECOVERED persisted for both;
            health back to FRESH.

  Honest notes on the run:
  * The dashboard reads came from THREE separate provider processes (one per phase of the
    harness). A fresh process has an empty dedup set, so it replays the snapshot's transition
    journal and re-logs flips it did not personally observe — which is why the final log listing
    shows the STALE pair twice. Within a SINGLE provider lifetime the once-per-flip discipline
    held exactly (the stale-phase process wrote exactly 2 records across 5 polls). Production
    runs one long-lived provider, so this is a harness artifact — but a dashboard RESTART would
    re-log journal history into the audit trail. Errs toward more audit, never toward a false
    all-clear. Worth a follow-up, not a blocker.
  * With the feed cut, each step fails instantly (ECONNREFUSED) and the loop spins with no
    backoff — it burned a 400-step budget in seconds during the first attempt. Not a
    correctness problem (every step still evaluates, which is what keeps the verdict honest),
    but a pacing follow-up for a long outage.
  * 5 of ~150 steps logged "poll budget expired midway through an inbound frame; dropping the
    session so the stream resynchronizes" at poll-budget 500 ms. Session 2's R3 guard doing its
    job; a 1500 ms budget showed far fewer. Tune the budget for production.

JUDGMENT-CRITIC ROUND 8 (reviewer=codex) — verdict BLOCK, and it is recorded as BLOCK.
  Both findings are architectural scope residuals on UNBUILT features, not in-scope defects:
  [high] subscription ownership — unchanged, deferred to SRS-MD-001 (empty struct today).
  [high] ACCOUNT SCOPE, new this round: IbLiveTickSource always builds IbAccountKind::Paper
    (the transport refuses Live pending SRS-EXE-001), so the `ib_gateway` cell reports the
    PAPER endpoint and must not be read as the broker connection real execution uses. Now
    recorded in heartbeat_freshness_contract.live_feed, at the IbLiveTickSource::connect call
    site, and at the dashboard mount point.
  Codex's own next_steps: "Treat the branch as serialized/incomplete unless these runtime path
  mismatches are resolved or explicitly excluded from production health." That is exactly the
  disposition taken, under explicit operator authorization: integrate serialized, passes STAYS
  FALSE, the mount opt-in and unset by default. No APPROVE was faked.

WHY passes STAYS FALSE even though all four legs passed live: the legs were demonstrated on a
PAPER gateway and on the daemon's OWN reqMktData lines. Until SRS-MD-001 owns the subscriptions
strategies consume and SRS-EXE-001 provides the live-account path, a green cell here does not
mean the platform's market-data and broker paths are healthy — only that these ones are.

RESUME / NEXT: flip to complete once MD-001 and EXE-001 land and the feed is re-pointed at the
consolidated stream + the live-designated gateway; re-run the live window above (SSH-forward
recipe included) and then close_feature.py SRS-MD-003 --verified. Everything else is built and
adversarially converged — do NOT rebuild it.

=== SESSION SRS-MD-003 (4) — the three live-run follow-ups, and a scheduler deadlock, 2026-08-09 ===
Session-4 outcome: serialized (passes STAYS false). The first `Outcome:` line at the top of
this file is unchanged on purpose — `serialized_notes()` reads only that one.
Branch: agent/SRS-MD-003, based on origin/main (0790258), 2 commits.

ALREADY-BUILT PROBE FIRST — nothing was rebuilt.
  Verified the session 1-3 claims against the tree rather than trusting them: live_feed.rs,
  live_market_data.rs, heartbeat.py, md003_heartbeat_cli, md003_live_feed_cli, the
  ATP_MD003_SNAPSHOT mount and 5 test files across L1/L4/L7 are all present and on main; the
  branch started at origin/main with 0 commits ahead. All four AC legs were proven live on
  2026-08-03. This session added only what the live run itself recorded as unfixed.

** THE OPERATOR DECISION THIS SESSION SURFACED — a four-feature deadlock. **
  MD-003's flip is gated on SRS-MD-001 (subscription ownership) and SRS-EXE-001 (account
  scope). Both reach SRS-PERF-001, which is blocked on SRS-MD-003:
      SRS-MD-003 -> SRS-MD-001 -> SRS-PERF-001 -> SRS-MD-003
      SRS-MD-003 -> SRS-EXE-001 -> SRS-PERF-001 -> SRS-MD-003
  So MD-003, MD-001, EXE-001 and PERF-001 can never go green. Worse, the prescribed fix is
  unavailable: `agent_pool.py block` drops cycle-forming edges with a warning on STDERR and
  still exits 0 (tools/agent_pool.py:1062), so MD-003's real blockers cannot be recorded and
  it returns to the ready frontier every cycle. That is rule 24's churn loop with rule 24's
  remedy missing; now written up as pipeline-and-integrate rule 31.
  NO GRAPH EDIT WAS MADE — the operator chose report-only. Two candidate remedies:
    (a) Drop the SRS-PERF-001 -> SRS-MD-003 edge IF PERF-001 needs MD-003's code (which is
        on main) rather than its flip. Would unblock PERF-001 -> MD-001/EXE-001 -> MD-003.
    (b) Record the 2026-08-03 live window via `evidence.py record --attested-by operator`
        (AGENTS.md's second legitimate path to a green) and force-complete from that end.
  ALSO: before this session there was NO evidence record at all — the 2026-08-03 live run
  was never captured through the gate. Steps 1-2 are now recorded; 3-4 still need live IB.

WHAT I BUILT — the three follow-ups the live run recorded, plus what review found under them.
1. PACING. The loop had none: `md003_live_feed_cli` relied on the tick drain blocking for its
   poll budget, which a refused connection does not do, so a cut feed spun a 400-step budget
   away in seconds during exactly the outage this feature reports. Added `degraded_backoff` +
   `LiveFeedLoop::pace_after`. The whole rule lives on the loop, next to the timing state:
   only a failed DRAIN is paced (a failed probe already spent its wire deadline and is
   self-pacing), and the pause is kept strictly short of the next probe instant so a pause
   and a probe never share one snapshot-write interval.
2. AUDIT DE-DUPLICATION ACROSS A RESTART. `_logged_transitions`/`_logged_stale` are
   per-process while the producer's snapshot carries a journal of past flips, so a restarted
   dashboard re-announced incidents it never observed (the live run's log listing showed the
   stale pair twice). The durable trail is now its own authority: the
   `md003:{feed}:{evaluated_at_ns}` correlation ids already written are read back once on
   first use via the existing `JsonlLogStore.read()`. An unreadable trail falls back to
   re-announcing rather than risk suppressing a record that was never written (CLAUDE.md
   rule 3, with the asymmetry stated: duplication is the safe direction here), and reports
   it as `audit_dedup_seeded: false`.
3. POLL BUDGET 500ms -> 1500ms. At 500 ms the live window took 5 mid-frame budget expiries in
   ~150 steps, each dropping and rebuilding the IB session.
4. (FROM REVIEW) THE WRITE-INTERVAL BUDGET. What must stay under 15 s is the interval between
   snapshot WRITES — the dashboard ages the file by max_age_ms and reports UNAVAILABLE beyond
   it, which would replace the per-feed stale evidence the AC requires. Now: `LiveFeedLoop::new`
   and the CLI refuse `poll_budget + cadence >= threshold` (both individually legal at
   14999/14998 and still over); `broker_probe_deadline(poll_budget, send_allowance)` derives
   the probe's reply deadline from what is left of the budget and `IbLiveTickSource::connect`
   hands it to the transport instead of the generic 15 s `IB_OP_DEADLINE`; the send allowance
   (`IB_CONNECT_TIMEOUT`, the socket write timeout) is a PARAMETER because atp-market-data may
   not name the adapter; and a budget leaving the reply no time is refused rather than started
   with a zero deadline that would fail every probe and report staleness it manufactured.

THE RESIDUAL — NOT CLOSED, operator-authorized stop (round 7).
  The op deadline is per-OPERATION, so one step whose drain internally reconnects and
  re-subscribes N lines can spend it per operation and then again on the probe; the composite
  can still exceed the threshold. Every SINGLE-operation path is now bounded, and this is
  strictly better than the pre-change state — origin/main used the generic 15 s deadline, so
  one probe timeout alone already exceeded the whole budget. But bounding operations pairwise
  does not enumerate to a close. The structural remedy the reviewer named twice (R3, R7) is to
  make the write interval independent of step duration: write an interim snapshot BEFORE the
  long I/O. That reorders evaluation and event publication inside `LiveFeedLoop::step` — the
  once-per-flip/journal surface sessions 1-3 spent ~20 rounds converging — and cannot be
  verified live from a parallel worktree, so it is deferred rather than attempted blind.
  Recorded in heartbeat_freshness_contract.live_feed.write_interval with the remedy.

What I tested (per step):
  Step 1: PASS — ./init.sh -> "✓ Environment ready" (recorded via evidence.py run at HEAD).
  Step 2: PASS — 66 tests over the fixture CLI, provider, boundary wiring, contract and
    domain suites (recorded via evidence.py run at HEAD).
  Step 3: NOT RUN — needs a live IB session (single-live invariant; cannot run in a parallel
    worktree). The four AC legs were proven live on 2026-08-03 by session 3; this session did
    not re-witness them and does not claim them.
  Step 4: passes STAYS FALSE.
  Gates: cargo test --workspace 2121 passed / 0 failed. cargo clippy --workspace -- -D warnings
    (the form CI runs) clean. rustfmt clean on the 3 touched .rs files (never a whole-crate
    fmt). pytest -m "not integration and not e2e" 4775 passed / 1 failed. ruff check+format and
    mypy clean on the touched .py paths.
  TWO PRE-EXISTING REDS, both reproduced on a PRISTINE origin/main worktree, neither mine:
    * tests/unit/test_docs_link_check.py — AGENTS.md:68 `tools/.agent_runtime.json` and
      prompts/initializer_prompt.md:135 `.git/hooks/pre-commit` "do not exist". This is a
      WORKTREE-ONLY artifact, NOT a red main: both paths exist in the primary checkout
      (the lease file is gitignored so it lives only there, and `.git` is a real directory
      there but a FILE in a worktree, so the literal path cannot resolve). Verified: the
      check exits 0 on `main` in the primary checkout and fails on a PRISTINE origin/main
      worktree. So `tools/run_ci_locally.sh` cannot go fully green from any worktree, and
      that is true of every agent branch, not this one. Worth an operator fix in the
      checker (skip runtime-generated paths, or resolve `.git` via `git rev-parse
      --git-common-dir`) so the mirror means what it says from a worktree.
    * cargo clippy --all-targets: crates/atp-orchestrator/tests/resv_3_trigger_log_schema.rs:14
      "doc list item overindented" — byte-identical to origin/main (from SRS-DATA-015). CI runs
      clippy WITHOUT --all-targets, so the enforced gate is green.

Critic verdicts:
  deterministic: APPROVE — no findings (run before each commit).
  judgment (adversarial_review.py, reviewer=codex): 7 rounds, final verdict BLOCK, recorded
    as BLOCK. No APPROVE was faked.
Adversarial rounds: 7   (baseline: 9, 10, 13, 13, 13, 14, 15, 20, 38)
  R1 block [high] backoff capped at cadence, but pause + the NEXT drain is what the dashboard
     ages the file by; cadence=14999/poll=14998 passes both individual guards and still leaves
     the snapshot over the 15 s age guard. CLASS: per-parameter timing validation that never
     checks the SUM. Fixed at the config gate (LiveFeedLoop::new, inherited by every driver)
     AND mirrored in the CLI so it fails on arguments before opening a socket.
  R2 block [high] pacing a failed BROKER PROBE adds delay to a path that never spun — it had
     already spent its wire deadline. Fixed: added `FeedStep::poll_failed` to tell a failed
     drain from a failed probe (source_error alone cannot), and paced only the drain.
  R3 block [high] a paced step followed by a step that runs a DUE probe puts the pause and the
     probe in one interval. Fixed: `pace_after` caps the pause strictly short of the next probe
     instant, so the two bounds compose instead of adding.
  R4 block [high] the probe ran on the transport's generic 15 s IB_OP_DEADLINE — equal to the
     entire staleness budget, so every interval containing it was over by construction. Fixed:
     `broker_probe_deadline` derives it from what is left of the budget; the composition layer
     hands it to the transport. The digest-pinned interactive_brokers.rs was NOT touched.
  R5 block [high] (a) the reply deadline does not cover getting the REQUEST out; a wedged peer
     can block the send for the socket write timeout. Fixed: `send_allowance` parameter, given
     `IB_CONNECT_TIMEOUT` by the composition layer; a budget with no room left is refused, not
     run with a zero deadline. (b) evidence covers only steps 1-2 for a live-ib feature — its
     own remedy was "merge only as serialized with passes left false", which is this
     disposition; recorded, not treated as a defect.
  R6 block [high] the evidence record's stamped commit was orphaned by `git commit --amend`, so
     it did not provably exercise the reviewed tree. Fixed by ordering: commit the code, record,
     then commit the record separately (its head is now an ancestor of HEAD). Now playbook rule 32.
  R7 block [high] the per-OPERATION deadline can be spent by a reconnect/resubscribe inside the
     drain and again by the probe. Real, and the documented stop: see THE RESIDUAL above.
     Operator authorized stopping here and integrating serialized.

Mutation verification (CLAUDE.md rule 6): all 4 added Python tests go red with the source
  reverted (`mutation_verify.py` exit 0). The tool cannot cover the Rust tests — they live in
  the file it reverts — so each was mutation-verified BY HAND, one property per mutation, and
  each died to exactly one: always-return-zero killed the growth test, cap-removed killed the
  threshold test, zero-guard-removed killed the healthy-path test, and 1500->500 killed the
  default-pin test. Two harness defects found doing this, both now playbook rules 22 and 24:
  mutation_verify run under the SYSTEM python3 (no pytest) reports every added test as
  "cannot fail", and its added-test regex crosses newlines so an added blank line before an
  existing `def test_` attributes that test to your range.

Playbook updates: docs/playbooks/pipeline-and-integrate.md rules 31 (block cannot record a
  downstream blocker, and exits 0 anyway) and 32 (record evidence after the final commit, in
  its own commit); docs/playbooks/test-integrity.md rules 22, 23, 24 (mutation_verify needs the
  venv interpreter; it cannot see tests living in the file it reverts; its added-test regex
  crosses newlines).

Resume / next:
  1. OPERATOR: break the deadlock — remedy (a) or (b) above. Nothing in this cluster can close
     until then, and MD-003 will keep being re-offered.
  2. The write-interval residual: the interim-snapshot restructuring, in a session with a live
     IB window (SSH-forward recipe in session 3 part 2).
  3. Unchanged deferred owners: MD-001 subscription ownership, EXE-001 account scope, MD-004
     probe bridge, MD-005 suppression, MD-006/NOTIF-001 routing, MD-007 live gap detection.
  Do NOT rebuild the monitor, fixture CLI, provider, live feed loop, IbLiveTickSource, the
  dashboard mount, or this session's pacing/de-duplication work.
