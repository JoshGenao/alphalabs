=== SESSION SRS-NOTIF-001 ===
Date: 2026-07-01
Feature: SRS-NOTIF-001 — notify the operator through email and SMS for IB
connectivity loss and critical failures (SyRS SYS-46, NFR-P6 ≤60,000ms; StRS
SN-1.12, SN-2.04, SC-9).
Outcome: serialized (core done + fault-injection tested; passes stays false —
the feature's own Step 4 says leave passes:false until real end-to-end delivery
over SMTP/SMS providers is proven).

What I did:
Built the real core notification dispatcher in the atp-notification crate (was a
stub). Std-only, zero external deps, Rust per AC-16/C-12. Four modules:
  * event.rs — vocabulary. NotificationTrigger (ConnectivityLoss / CriticalFailure
    + detection instant in MILLISECONDS — NFR-P6 is ms, so whole-seconds would let
    a 60,001ms dispatch round down and wrongly pass), NotificationSeverity
    (ERROR/CRITICAL), NotificationChannel (Email/Sms), REQUIRED_CHANNELS, and the
    OPAQUE ChannelDelivery + NotificationEvent (private carriers, pub(crate)
    ctors — a delivery status can only be minted by the dispatcher from a REAL
    send: no-fabrication by construction). NotificationEvent exposes
    dispatch_latency_millis() / within_dispatch_sla() (≤60_000ms). ChannelDelivery
    carries NO per-channel timestamp (would be false precision; the single honest
    SLA anchor is the event's dispatch_began_at_millis).
  * channel.rs — NotificationChannelClient port; send(&self, msg, deadline) — the
    per-channel deadline is a MANDATORY API parameter. Typed ChannelError
    taxonomy: Unconfigured / TransportUnavailable / Timeout / Rejected. Concrete
    SMTP (IF-10) / SMS gateway (IF-11) adapters live in atp-adapters (deferred).
    No vendor SDK in core; NotificationMessage + NotificationEvent carry no
    credential (NFR-S4).
  * dispatcher.rs — OperatorNotifier: detection→dispatch→record. Injected clock
    (deterministic). Enforces: required email+SMS fan-out exactly once
    (MissingRequiredChannel / DuplicateChannel), reversed-timestamp rejection
    (DispatchBeforeDetection — dispatch can't precede detection), the SYS-75
    fail-safe (a CriticalFailure is NEVER suppressed; suppression only silences
    ConnectivityLoss during a restart window). Passes its per-channel deadline to
    every send (clamped to MAX_CHANNEL_DEADLINE = 60_000ms / 2 required channels =
    30_000ms so sequential fan-out fits the budget); a channel returning Timeout is
    recorded Failed and the other channel is still attempted. (Hard-cancel of an
    adapter that IGNORES its deadline needs async/cancellable transport = out of
    the zero-dep baseline; the adapter owns its cancellable socket timeout —
    verified at the deferred integration.)
  * store.rs — durable append-only NotificationEventStore. Atomic
    scratch→fsync→rename→dir-fsync + FNV-1a-checksummed fail-closed codec.
    NotificationStoreLock (O_EXCL) + append_durably() serialize concurrent writers
    (no lost events — several sources emit notifications). Missing dir = Io; missing
    file = fresh empty; corrupt/truncated = ChecksumMismatch/Corrupt. READ↔WRITE
    VALIDATION SYMMETRY on restore: a checksum-valid but semantically-impossible
    blob is rejected (reversed timestamps; missing/duplicate required channel;
    suppressed CriticalFailure; mixed suppression) — the audit trail can't be made
    to lie. Untrusted counts never pre-size an allocation.

Key decisions:
  * Scoped to atp-notification (+ one paired L7 domain test). The kill-switch /
    Hot-Swap / orchestrator sinks / log ERROR-CRITICAL filter / API alerts shape
    already DEFER their email/SMS fan-out to SRS-NOTIF-001; wiring them is at the
    composition root (dependency direction), not by making lower crates depend on
    atp-notification. Those deferred hooks in other features' runtime_services.json
    stay accurate (I am NOT flipping passes:true) — no repo-wide metadata sweep.
  * Config keys ATP_SMTP_API_KEY / ATP_SMS_API_KEY already exist (secret, NFR-S4).

What I tested (per step):
  Step 1: PASS — ./init.sh → "Environment ready".
  Step 2/3 (exercise + AC): PASS — cargo test -p atp-notification → 25 integration
    + fault-injection tests + 1 lib test, all green. Proves within-60_000ms
    dispatch + delivery-status stored; 60_000ms passes / 60_001ms breach; reversed
    timestamps rejected; no-fabrication of a failed channel; all-channels-failed
    still stored; required email+SMS enforced (empty/email-only/sms-only/dup
    rejected); a Timeout channel recorded Failed while the other delivers; deadline
    threaded + clamped; suppression seam; critical-never-suppressed; durable
    round-trip in insertion order; concurrent-writer no-loss; fail-closed codec
    (corrupt / foreign / missing-dir) AND checksum-valid-but-semantically-invalid
    restore rejections. Paired L7: pytest tests/domain/test_notification_dispatch.py
    → 16 passed (shells cargo test).
  Step 4 (evidence + hold passes false): serialized — the fault-injection /
    integration method against real SMTP/SMS providers cannot run solo in parallel;
    passes stays false pending operator end-to-end verification.
  Gate: cargo test --workspace 0-failed; cargo clippy --workspace clean; cargo fmt
    --check clean; cargo doc clean; pytest -m "not integration and not e2e" → 2626
    passed, 4 pre-existing skips; deterministic critic APPROVE. NOTE:
    run_ci_locally.sh's mypy step is PRE-EXISTING red on python/atp_strategy/
    examples/ (66 errors, identical to origin/main, untouched by this diff; mypy
    does not scan tests/domain/). Not a regression from this feature.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (tools/codex_review.sh origin/main): 9 completed rounds, each finding
    addressed in-scope — required-channel enforcement; concurrent-writer no-loss;
    reversed-timestamp rejection; false-per-channel-timestamp removed; mandatory
    deadline API param + typed Timeout; millisecond SLA precision; restore
    read↔write validation symmetry (reversed ts / missing channel / suppressed
    critical / mixed suppression); several doc-drift fixes. The timeout-hang
    finding converged (deadline-in-API + adapter cancellable-I/O contract). Round
    10 could not return a verdict — Codex hit its account usage limit (resets
    ~12:54 PM). Substituted an INDEPENDENT fresh-context sub-agent review per the
    AGENTS.md fallback (prompts/critic_prompt.md schema): verdict APPROVE, no
    findings — it independently verified the injected clock (no SystemTime::now),
    no falsifiable SLA evidence, restore rejects all 4 semantically-impossible
    blob classes, email+SMS enforced, no credential leak, the O_EXCL
    concurrent-writer lock, and std-only/one-way-dep hygiene; the only residual
    (hard-cancel of an adapter that ignores its deadline) is architecturally
    forced in a synchronous zero-dep core and honestly deferred to the adapter
    integration, which its instructions say is not grounds to block.

Resume / next:
  SRS-NOTIF-001 stays passes:false. To flip it (operator, --mode complete or the
  verified-e2e label): implement the concrete SMTP + SMS adapters in atp-adapters
  (reading ATP_SMTP_API_KEY / ATP_SMS_API_KEY, cancellable connect/send timeouts
  mapping to ChannelError::Timeout), wire real detection (execution connectivity
  gate ERR-2/SRS-SAFE-003 → NotificationTrigger::connectivity_loss; CRITICAL system
  events → ::critical_failure) + the SYS-75 restart-window suppression decision
  (SRS-MD-005), then run the fault-injection + integration test proving a real IB
  connectivity loss dispatches email+SMS within 60s and the delivery status is
  stored. The seam here (OperatorNotifier + NotificationChannelClient +
  NotificationEventStore + append_durably) is what those consumers plug into.
  Downstream unblock when NOTIF-001 flips: ERR-7, ERR-8 (both blocked-on
  SRS-NOTIF-001).

=== SESSION 2026-07-31 (operator-directed: build the transports) ===
Outcome: still serialized (passes:false). The two missing transports now EXIST and are
verified over real sockets; what remains is a real provider behind them, the detection
wiring, and a dispatcher process. Steps 3-5 of the agreed 6-step plan are NOT done.

Context for this session: the operator asked which awaiting-verification features need
only a verified-e2e label, and chose SRS-NOTIF-001 off that audit — not because it is
closest to green (it is not; it needed an ordinary build) but because it is the highest
-impact blocker on a deadlocked board (ERR-7, ERR-8, MD-005, PERF-001, DATA-010/019/020,
UI-1 all wait on it).

## What I built

1. prep (7d464fe): `atp-adapters` may now depend on `atp-notification` in
   runtime_services.json's dependency_direction allowlist. Standard hexagonal direction
   (the adapter depends on the crate owning the port). No cycle: atp-notification stays
   in lower_layer_scan_crates with the `atp_adapters` token forbidden, so the reverse
   edge is statically impossible. GOTCHA: that scan is a raw SUBSTRING match over source
   lines, so writing `atp_adapters::notification` in an atp-notification DOC COMMENT
   fails the check. Spell it `atp-adapters` (hyphen) in that crate.

2. feat (f8e69f9): crates/atp-adapters/src/notification{.rs,/smtp.rs,/sms.rs}.
   NEW FILES ONLY — interactive_brokers.rs + wire.rs are SHA-256 digest-pinned by
   tools/ib_adapter_check.py and editing them would flip closed-green EXE-006 red.

### The architectural decision that shapes everything else
The workspace has ZERO external crates → no TLS available to a std-only adapter. Rather
than break that invariant tree-wide, the TLS boundary became a DEPLOYMENT component:
`phase1-notification-egress` owns the authenticated TLS session to the provider; the
adapters speak plaintext to it on an internal network. Enforced, not just documented:
  * EgressEndpoint re-resolves PER CONNECT and refuses any host not resolving to
    loopback/RFC1918, validating EVERY resolved address (a [private, public] round-robin
    would otherwise pass on the first) and unwrapping IPv4-mapped IPv6.
  * Each adapter authenticates with its catalogued secret, and the email transport
    REFUSES a relay that does not advertise AUTH — an open relay means any container that
    can route to it can forge operator alerts.

### The deadline residual is now discharged (this was the real work)
channel.rs had documented "the adapter honours the deadline via cancellable I/O" as
verified-at-the-deferred-integration. Two ways that is silently wrong, both now pinned:
  * PER-OPERATION vs TOTAL budget. A socket timeout is per-op; arming `deadline` on each
    of SMTP's ~8 round trips licenses 8x the budget, making the dispatcher's
    MAX_CHANNEL_DEADLINE = DISPATCH_SLA_MS/2 arithmetic meaningless. SendBudget is created
    once per send; every op is armed from what is LEFT.
  * BETWEEN-READ re-checking. Arming once and calling read_line/read_to_end is NOT enough:
    the timeout countdown restarts on every read syscall, so a relay dribbling one byte
    per interval never trips one and holds an operator alert forever while every timeout
    looks correctly armed. Both paths drive fill_buf with the budget re-checked between
    reads (read_line_budgeted / read_to_end_budgeted). std's read_line/read_to_end are
    UNUSABLE on a deadline path for this reason — don't "simplify" them back in.
  * METHOD NOTE: I verified both tests discriminate by temporarily writing each bug in.
    Each is caught by EXACTLY ONE test and no other. A test that cannot fail is not
    evidence — worth doing for any property whose wrong implementation still passes
    everything else.

Also: multiline SMTP replies incl. a code changing mid-reply (else a 550 reads as the
following 250); dot-stuffing; 4xx/5xx as distinct remediations; CR/LF folded out of
interpolated protocol lines, but envelope addresses / HTTP path / credential REFUSED
(a subject can be repaired, an address cannot); hand-rolled base64 + JSON escaping;
bounded lines/response/SMS body; character-boundary truncation (byte-slicing a non-ASCII
alert body would panic the notification path); hand-written Debug redacting both configs.

Deleted a `command_redacted` wrapper I had written that was byte-identical to `command` —
security theater. Replaced with the real guarantee: errors interpolate `stage` and the
relay's own reply, never the command line, pinned by
a_refused_credential_never_appears_in_the_error_detail.

## What I tested
  17 L4 boundary (crates/atp-adapters/tests/srs_notif_001_transports.rs) over REAL
  loopback sockets against scripted SMTP/HTTP relays; 26 L1 unit; 7 L7 domain
  (tests/domain/test_notification_transports.py — "notification" is NOT a SAFETY_PATH_RE
  token so the critic does not demand the pairing, but this is the connectivity/
  critical-failure path, so it is written anyway).
  Gate: cargo test --workspace 2060 passed/0 failed; pytest -m "not integration and not
  e2e" 4415 passed/3 pre-existing skips; clippy -D warnings clean; cargo fmt clean
  (formatted with `cargo fmt -p atp-adapters` — NEVER whole-workspace); dependency_boundary
  / architecture / adapter_isolation / ib_adapter / kill_switch_timeout /
  credential_security / config / deployment all PASS. Deterministic critic APPROVE.
  NOT YET RUN: the judgment critic (tools/adversarial_review.py) — next session must run it
  before this is integrated.

## Doc-drift swept
  atp-notification lib.rs + channel.rs and kill_switch_timeout_contract.deferred[3] all
  claimed these adapters do not exist. Rewritten to state what is genuinely left.

## Resume / next — the remaining 3 build steps, then the flip
  3. phase1-notification-egress sidecar (compose): terminates TLS to the providers, maps
     ATP_SMTP_API_KEY -> the real provider credential, exposes plaintext SMTP on 1025 and
     POST /sms on 8025 to the internal network only. Must AUTH its clients (the email
     adapter refuses it otherwise). Provider choice (operator, 2026-07-31): Brevo free
     SMTP relay (300/day, no card) for IF-10 — the operator also asked about Gmail, which
     works as either the relay login or the destination inbox. NO SMS PROVIDER CHOSEN YET;
     recommended Twilio trial ($15 credit, verified-number-only is fine since the operator
     is the sole recipient; note trial messages carry a "Sent from your Twilio trial
     account" prefix that WILL appear in the flip evidence). Two email providers cannot
     close this: REQUIRED_CHANNELS is Email AND Sms, enforced fail-closed, so a missing
     SMS channel raises MissingRequiredChannel and nothing sends at all.
  4. Detection wiring at the composition root: atp-execution records a ConnectivityEvent
     {Unreachable | ScheduledRestartWindow} through ConnectivityEventSink at
     crates/atp-execution/src/lib.rs:640 (the ERR-2/SRS-SAFE-003 gate). Bind that sink to
     OperatorNotifier::connectivity_loss. ScheduledRestartWindow IS the SYS-75 suppression
     decision, decidable right there. atp-orchestrator already deps both crates and
     already binds NotifierAlertSink -> OperatorNotifier for SAFE-002
     (kill_switch_timeout.rs) — follow that composition, do NOT make atp-execution depend
     on atp-notification.
  5. A real dispatcher process: phase1-notification-dispatcher currently runs
     core-runtime.Dockerfile's CMD, which is `cargo test --locked --workspace --lib`.
  6. Flip: on the Proxmox host stop the IB Gateway to force a genuine Unreachable, confirm
     a real email + real SMS inside 60s and that the stored NotificationEvent records both
     deliveries, then close_feature.py SRS-NOTIF-001 --verified.

## Branch hazard discovered (read before integrating)
  A CONCURRENT agent session was committing into this same working directory while I
  worked. Two commits I did not author landed on agent/SRS-NOTIF-001: ef66a72 (app.js
  RES-003 fix) and ed2c60f (a close_feature chore that flips passes:true in
  feature_list.json — a SHARED-STATE mutation `integrate` rejects from a branch). Both
  also exist on origin/main under different hashes (60579d0, dc230d9), so a rebase onto
  origin/main should drop them as duplicate patches — VERIFY that before integrating, and
  confirm 0 deletions vs origin/main. Branch also carries 9bc2b9d (someone else's conftest
  test-isolation fix) which is NOT yet on origin/main. See
  [[feedback_primary_worktree_may_be_on_agent_branch]].

=== SESSION 2026-08-01 (operator-directed: finalize + adversarial + integrate) ===
Outcome: STILL SERIALIZED (passes:false). Detection wiring + the operator CLI landed;
the alert path is now complete and verified end to end against a fake relay. What
remains for the flip is genuinely only operator-side: a real egress relay and a real
provider account. See "WHAT IS AND IS NOT PROVEN" below before claiming anything.

## Built this session (steps 4 + 5 of the 6-step plan)

3. connectivity_notification.rs (atp-orchestrator) — the DETECTION half that was missing.
   atp-execution already emitted the fact (ConnectivityEvent through ConnectivityEventSink
   at crates/atp-execution/src/lib.rs:640, the ERR-2/SAFE-003 gate); nothing implemented
   that port against the notifier. Lives at the composition root because atp-execution must
   not depend on atp-notification — same shape as kill_switch_timeout::NotifierAlertSink.
   Design points that took thought:
     * SYS-75 suppression is decided from the STATE. ConnectivityEvent carries BOTH a state
       and a `scheduled_restart` bool and they can disagree; a bare bool that silences an
       operator page is the forgeable-input shape, so suppression needs BOTH to agree and
       any disagreement PAGES. On an alert path the safe direction is to page.
     * COOL-DOWN (5 min). The sink fires once per BLOCKED ORDER, not per outage — a retry
       loop would drive one real SMTP conversation + one real SMS per attempt. Coalescing is
       never silent: the folded count rides in the NEXT alert's summary.
     * The cool-down is armed by the ATTEMPT, not by success — a provider outage is exactly
       when every send fails, and arming on success leaves a broken provider un-rate-limited.
     * Connected never fabricates an outage (and ends the episode); backwards clock step
       can't expire the window; poisoned mutex is recovered not panicked (this runs INSIDE
       the engine's rejection path); dispatch-ok-but-store-failed is recorded as a FAILURE.

4. notif001_operator_alert_cli (atp-orchestrator bin) — what the operator runs for the flip.
   Every layer below the arg parser is production: it drives the REAL
   ExecutionEngine::submit_live_order so the ConnectivityEvent comes from the real gate
   rather than being hand-built, through the real sink/dispatcher/transports/store. Broker
   port PANICS if reached (an ERR-2 regression is loud). Allow-list args; exits 0 only when
   every required channel terminal-succeeded AND the event was stored.

## Adversarial review (tools/adversarial_review.py, reviewer=CODEX)
  Round 1: BLOCK, 2 findings, BOTH legitimate and BOTH fixed:
    [high] Unbounded DNS bypassed the send deadline. I had DOCUMENTED this ("resolution is
      not cancellable in std, so the budget is re-checked after it returns") — which
      describes the hole rather than closing it. DNS is the step most able to wedge, and an
      unbounded resolver holds the whole alert send while every socket timeout still looks
      armed. FIXED: resolution runs on a worker thread, the WAIT is bounded by the remaining
      budget, overrun is ChannelError::Timeout; an IP literal parses directly and spawns
      nothing. NOT the detached-watchdog anti-pattern the core rejected — that one tries to
      cancel a wedged socket syscall it cannot reach and leaks a thread per notification on
      an unbounded path; this is a self-terminating DNS lookup on a path the sink already
      rate-limits. LESSON: "I documented the limitation" is not a fix, and a reviewer will
      say so.
    [medium] The branch carried an unrelated tests/conftest.py fixture change — a concurrent
      session's commit that landed on MY branch (see the branch hazard note in the previous
      session). FIXED by dropping it: `git rebase --onto origin/main f4990a8`. PRESERVED
      first on branch `fix/test-isolation-sandbox` — it is a GOOD fix (the autouse atp_*
      import sandbox) and still needs its own PR. Do not lose it.

## WHAT IS AND IS NOT PROVEN (read before claiming a green)
  PROVEN, end to end, against a local fake relay (transcript inspected, not just exit code):
    the REAL gate fired (gate=CONNECTIVITY_BLOCKED error_type=IbGatewayUnreachable) ->
    dispatched within SLA -> email Delivered carrying the relay's real queue id
    (FAKE-QUEUE-9001) -> sms Delivered carrying the gateway's accept id (SM-FAKE-4242) ->
    event durably stored (both deliveries in notification_events.store) -> exit 0.
    Negative path with the relay stopped: both channels TRANSPORT_UNAVAILABLE with the
    concrete reason, failure STILL stored, exit 1.
    The relay saw: AUTH PLAIN base64 (raw key absent), well-formed RFC 5322 message, and a
    JSON SMS body carrying the subject (SMS has no subject line).
  NOT PROVEN, and must not be claimed:
    * that a REAL IB Gateway outage drives this (the CLI takes the state as an operator
      assertion; only a real gateway stop proves the producer);
    * that anything reached a real mailbox or a real handset. Both transports prove hand-off
      to a RELAY. The relay image does not exist and no provider account exists.

## Resume / next — the ONLY things between here and passes:true
  1. Stand up phase1-notification-egress: terminates TLS to the providers, maps
     ATP_SMTP_API_KEY -> the real provider credential, exposes plaintext SMTP (1025) and
     POST /sms (8025) on the internal network. It MUST authenticate its clients — the email
     adapter refuses a relay that does not advertise AUTH. NOT BUILT: deliberately deferred
     rather than shipped untested, since it cannot be verified without credentials.
  2. Providers: operator chose Brevo (free SMTP relay, 300/day, no card) for IF-10 and asked
     about Gmail (works as relay login or destination inbox). NO SMS PROVIDER CHOSEN —
     recommended Twilio trial (~$15 credit; verified-number-only is fine as the operator is
     the sole recipient; trial messages carry a "Sent from your Twilio trial account" prefix
     that WILL appear in the evidence). TWO EMAIL PROVIDERS CANNOT CLOSE THIS:
     REQUIRED_CHANNELS is Email AND Sms enforced fail-closed — a missing SMS channel raises
     MissingRequiredChannel and NOTHING sends.
  3. On the Proxmox host: stop the IB Gateway for a genuine outage, run
     `notif001_operator_alert_cli outage --state unreachable --store <dir>`, confirm a real
     email and a real SMS arrive inside 60s and the stored event records both, then
     `close_feature.py SRS-NOTIF-001 --verified`.
  Still deferred beyond the flip: a long-running dispatcher process
  (phase1-notification-dispatcher still runs core-runtime.Dockerfile's `cargo test` CMD),
  and the CRITICAL-system-event -> critical_failure detection leg (SAFE-002 already binds
  its own critical path; ORCH/LOG CRITICAL events are unrouted).

## Gate
  cargo test --workspace 2092 passed / 0 failed; cargo clippy --workspace -D warnings clean;
  cargo fmt clean; 8 static checks PASS; deterministic critic APPROVE on every commit.
  NOTE: `cargo clippy --all-targets` (stricter than CI, which omits --all-targets) reports a
  PRE-EXISTING doc-lint in crates/atp-orchestrator/tests/resv_3_trigger_log_schema.rs — a
  test target CI does not lint, not mine, deliberately not fixed here.

=== ADVERSARIAL REVIEW 2026-08-01 (9 Codex rounds) + OPERATOR AUTHORIZATION ===

## The 11 in-scope defects Codex found and I fixed
  R1 [high] Unbounded DNS bypassed the send deadline. I had DOCUMENTED the hole
     ("resolution is not cancellable in std, so the budget is re-checked after it
     returns") instead of closing it. Now resolution runs on a worker and the WAIT is
     budget-bounded; an IP literal spawns nothing. LESSON: documenting a limitation is
     not fixing it, and a reviewer will say so.
  R1 [medium] Branch carried a concurrent session's unrelated conftest commit → dropped
     via `git rebase --onto origin/main f4990a8`, PRESERVED on branch
     `fix/test-isolation-sandbox`. That fix is GOOD and still needs its own PR.
  R2 [high] connect() used only resolved[0]. `localhost` is usually ::1 AND 127.0.0.1, and
     resolver order says nothing about who is listening — a relay bound to IPv4 behind an
     IPv6-first resolver was "unreachable" while running. Now tries all, same budget.
     Split into connect_validated(&[SocketAddr]) so a [dead, live] list makes it
     deterministic (DNS order is not controllable from a test).
  R4 [high] AUTH reply text was interpolated into a PERSISTED error → an echoing relay
     wrote a recoverable ATP_SMTP_API_KEY into the audit log. My existing test used a
     benign 535 and proved nothing.
  R5 [high] Same class on SMS (I had fixed the INSTANCE, not the CLASS).
  R5 [high] **Planned maintenance could silence a real outage** — the sharpest bug of the
     nine. A suppressed scheduled-restart record SENDS NOTHING but armed the shared
     cool-down, so a genuine Unreachable 60s later was coalesced and never paged. A
     restart window is exactly when a real failure is most likely and least
     distinguishable → worst moment to go quiet. Outage/maintenance now hold INDEPENDENT
     windows and suppression is decided BEFORE the cool-down.
  R6 [high] Link-local was in the egress allowlist with no justification. 169.254.169.254
     is the cloud metadata endpoint and both adapters send credential+body right after
     connect. Removed from both families incl. the IPv4-mapped form.
  R6 [high] SMS status line built errors BEFORE the body redaction ran (3rd instance).
  R6 [high] Notification I/O sat in FRONT of the reconnect — record() runs inside
     submit_live_order, which calls request_reconnect() immediately after, so inline
     dispatch put up to 2 channel deadlines between detection and recovery. Moved off the
     caller's thread + flush(timeout).
  R7 [high] SMTP SUCCESS receipt unredacted (4th instance of the same class — the happy
     path, where nobody looks for a leak).
  R7 [medium] My own spawn-failure fallback REINTRODUCED the inline block, worst exactly
     when it triggers (resource exhaustion). Removed; failure recorded instead.
  R8 [high] Arming the cool-down before the spawn meant a failed spawn bought 5 minutes of
     silence for a dispatch that never began. Now armed only on success, lock held across
     the spawn (also kills a double-dispatch race). Needed a `#[cfg(test)]` spawn-failure
     seam — thread::spawn cannot be made to fail on demand, and that branch decides
     whether a resource spike silences the operator.

  PATTERN WORTH KEEPING: rounds 6→7→8 were each a consequence of the PREVIOUS round's fix,
  all inside the async-dispatch design introduced in R6. When a fix creates the next
  finding twice running, the design is delicate — slow down and enumerate the states
  rather than patching forward.
  SECOND PATTERN: the credential leak took FOUR rounds because I fixed instances. The rule
  that would have caught all four at once: EVERY relay-controlled string reaching a
  persisted field gets scrubbed at construction — success paths included.

## The residual: why Codex still BLOCKs, and the operator authorization
  R3 and R9 are NOT defects in the diff. They are the reasons this feature is
  passes:false, restated as blocks:
    R3 [high] No automatic dispatcher runtime — phase1-notification-dispatcher still runs
       core-runtime.Dockerfile's `cargo test` CMD. Needs SRS-EXE-001 (there is no live IB
       inbound surface to subscribe to), so it is owned there, not stubbed here.
    R3 [high] General CRITICAL system-event stream unrouted. PARTIAL, not total:
       SRS-SAFE-002's NotifierAlertSink already dispatches the kill-switch
       liquidation-timeout CriticalFailure through this dispatcher. Owners for the rest:
       SRS-LOG-001 (ERROR/CRITICAL filter), SRS-ORCH-003 (workload/health).
    R9 [high] Detection is OBSERVATION-driven, not LOSS-driven. The trigger is a blocked
       live submission, so a Gateway dying while no order is routed goes unnoticed, and
       detected_at_millis is the observation instant — reading the stored latency as
       loss-to-dispatch would credit an NFR-P6 compliance not demonstrated. Fixed the
       HONESTY half (alert text + module/domain docs now say so). The structural half
       needs a producer WATCHING the gateway: owners SRS-MD-003, SRS-EXE-001. Codex's own
       next_step says "do not close or ship as satisfying the connectivity-loss leg" —
       i.e. exactly passes:false.

  OPERATOR AUTHORIZATION (AskUserQuestion, 2026-08-01): operator authorized
  `integrate --mode serialized` over the standing BLOCK, on the basis that every remaining
  finding is deferred scope with a named owner and none is a defect in this diff. Recorded
  honestly — the judgment critic's verdict is BLOCK, NOT approve, and was never
  represented otherwise. Same shape as the DATA-010 / DATA-013 operator-authorized
  serialized landings.

## Gate at integrate time
  cargo test --workspace 2107 passed / 0 failed; pytest -m "not integration and not e2e"
  4527 passed / 3 pre-existing skips; cargo clippy --workspace -D warnings clean; fmt
  clean; deterministic critic APPROVE on all 14 commits.
  GOTCHA: a mid-session full run showed 4 failures in atp-data::access_journal — a crate I
  never touched. PHANTOM: the concurrent agent session was running cargo test at the same
  time and the fixed-name scratch dirs collided (see [[feedback_no_concurrent_cargo_test_runs]]).
  22/22 in isolation, 0 failures on a clean re-run. Do not chase these.

=== SESSION 2026-08-17 (operator-directed: replace the SMS channel with push/ntfy) ===
Outcome: STILL SERIALIZED (passes:false). The provider blocker that kept this
feature un-verifiable for three sessions is GONE — a real operator alert now
reaches a real push server, driven by the real gate. The two STRUCTURAL blockers
(R3/R9 below) are untouched and remain the reason passes stays false.

## Why the channel changed (operator decision, 2026-08-16)
US A2P 10DLC registration is weeks of lead time and carriers filter unregistered
traffic silently, so an SMS channel could never be PROVEN to deliver — three
sessions failed to close this feature for exactly that reason. IF-11 is now push
notification to a self-hosted ntfy on the LAN (RFC 1918), reached from the
operator's phone over VPN. SC-9 ("at least two configured channels") unchanged.

## Step 0: ntfy verified EMPIRICALLY before any code was written
`curl -v` against ntfy.sh and a local `binwiederhier/ntfy`. Two findings the
documentation does NOT give, and both changed the design:
  * THE 4,096-BYTE ATTACHMENT THRESHOLD IS INCLUSIVE. Docs say "greater than";
    a body of EXACTLY 4,096 bytes already converts to a file. On conversion the
    request still returns HTTP 200 with a valid message id while `message`
    becomes "You received a file: attachment.txt" — the alert text never reaches
    the lock screen while the transport reports success. So MAX_PUSH_BODY_BYTES
    (1,024) is a SAFETY property, and a 2xx carrying an attachment is recorded a
    FAILURE, not a delivery.
  * AN EMPTY BODY BECOMES THE LITERAL WORD "triggered". ntfy substitutes its own
    placeholder; an empty composition now sends an explicit marker instead.
  Auth taxonomy, all confirmed live: 200 + JSON{id} success; 401 wrong/revoked
  token; 403 missing token OR token without publish access to the topic; 400
  malformed request (e.g. out-of-range X-Priority). 400/401/403 -> Rejected or
  Unconfigured (setup faults); 5xx/408/429 -> TransportUnavailable.
  Full transcript: the scratchpad ntfy_evidence.md written during the session.

## What I built
1. prep (ec4143f) — config catalogue. ATP_SMS_API_KEY -> ATP_PUSH_TOKEN +
   ATP_PUSH_TOPIC (both secret) + ATP_PUSH_HOST/PORT. Added KeyType.STRING.
   ALSO FIXED THE PRE-EXISTING BUG IN FULL: ATP_OPERATOR_SMS was read by
   from_env but absent from the catalogue so deployment_check.py never validated
   it; ATP_OPERATOR_EMAIL had the identical defect and is catalogued too.
   Replaced the `endswith("_API_KEY")` filter in the placeholder-secret tests
   with a set derived from the catalogue's `secret` flag — that filter had
   silently stopped covering the notification channel the moment its keys were
   renamed, which is the exact drift those tests exist to catch.
2. feat (642cd00) — push.rs replaces sms.rs; BOTH channel enums renamed
   (NotificationChannel::Sms and atp_types::OperatorAlertChannel::Sms -> Push),
   together with the four pinned OperatorAlertChannel contracts in
   runtime_services.json that hot_swap_demotion_check / kill_switch_timeout_check
   read. Store schema 1->2 ("S" tag -> "P"); MIN_SUPPORTED moved to 2 as well so
   a v1 blob is refused by VERSION (precise) not as a corrupt tag (vague), and
   the SRS-DATA-015 schema registry was updated in lockstep (it caught the bump).
   Docs: IF-11, SYS-44b, SYS-46, SYS-49c, NFR-P6, NFR-S4, SN-1.12, data
   dictionary, cost model, context diagram + dated revision notes in all three
   requirement documents.
3. fix (bfab7ce) — the adversarial-review defect, below.

## THE TOPIC IS A CREDENTIAL — and it is why the review found a real bug
On ntfy, holding the topic is enough to publish, so ATP_PUSH_TOPIC is catalogued
secret. Two consequences that are easy to get wrong:
  * The topic is in the REQUEST LINE, and ntfy's 2xx body ECHOES it. On this
    transport the SUCCESS path is the one that reliably carries a credential.
    Config rejections name the offending key and never interpolate its value
    (sms.rs wrote `got {path:?}` — harmless for a fixed "/sms" literal, a leak
    the moment the field holds the topic).
  * REDACTION AND PARSING DO NOT COMMUTE. I first redacted the response body and
    parsed the REDACTED text. Redaction is a blind substring replace and the
    topic is operator-chosen, so a topic of literally `attachment` rewrote the
    JSON KEY `"attachment"` to `"<redacted>"` and the silent-conversion guard
    never fired — an alert ntfy had turned into a file would have been stored as
    DELIVERED. A topic of `id` corrupted the reference lookup the same way.
    FIXED: structure is read from the RAW payload, only extracted VALUES are
    redacted before persistence. LESSON WORTH KEEPING: blind substring redaction
    over a structured document can rewrite its structure — it belongs on the way
    OUT (values being persisted), never on the way IN (before the document is
    understood).

## Discriminating-test method (a test that cannot fail is not evidence)
Each property verified by writing the bug in and confirming exactly the right
test fails:
  * cap by characters instead of bytes -> a_character_cap_would_not_bound_what_
    ntfy_actually_measures, and nothing else. NOTE: my FIRST version of that
    test did NOT discriminate — it used 4,000 characters, which exceeds BOTH
    limits and so passes under either unit. The units only disagree between
    1,024 bytes and 1,024 characters, so the test now uses 700 'é' (700 chars /
    1,400 bytes). Worth remembering: for a unit-of-measure bug the test input
    must sit in the band where the two units disagree.
  * topic dropped from the redaction set -> the two topic-scrub tests.
  * Debug printing the topic -> the config Debug test.
  * redact-then-parse ordering restored -> exactly the two new collision tests.

## What I tested (per step)
  Step 1: PASS — ./init.sh -> "Environment ready". NOTE: init.sh installs only
    requirements.txt, never requirements-dev.txt, so pytest was absent from the
    worktree venv; installed it (environment fix, in scope).
  Step 2 (exercise): PASS — 29 L4 boundary over real loopback sockets against a
    scripted ntfy; 65 L1 unit; L7 domain updated (test_notification_transports,
    _dispatch, _connectivity_notification, _kill_switch_liquidation_timeout,
    _credential_redaction).
  Step 3 (AC — dispatch within 60s + delivery status stored): PASS, against a
    REAL ntfy container driven by the REAL ERR-2 gate:
      gate=CONNECTIVITY_BLOCKED error_type=IbGatewayUnreachable
      dispatched=true  dispatch-latency-ms=0  within-sla=true  stored=true
      email=Delivered detail=2.0.0 Ok: queued as FAKE-QUEUE-9001
      push=Delivered  detail=2wJaD92j4gMe   <- ntfy's REAL message id
      alert-path-ok=true, exit 0
    Store INSPECTED, not just the exit code: schema version 2, channel tags E
    and P, and neither the token, the topic, nor the SMTP key present anywhere
    in the file.
    Negative path with ntfy stopped: push=Failed with the concrete connect
    error, the FAILURE still durably stored, exit 1.
  Step 4 (evidence + hold passes false): serialized — see below.
  Gate: cargo test --workspace 2121 passed / 0 failed; pytest -m "not
    integration and not e2e" 4528 passed / 5 pre-existing skips; clippy
    --workspace -D warnings clean; cargo fmt clean (formatted per-crate, NEVER
    whole-workspace); 12 static checks PASS (architecture, config, deployment,
    credential_security, hot_swap_demotion, kill_switch_timeout,
    adapter_isolation, dependency_boundary, connectivity, data015_schema,
    perf_measurement, sim_halt).
  PHANTOM, do not chase: one mid-session pytest run failed
    test_outbox_reconciliation because a concurrent cargo test --workspace
    collided on fixed-name scratch dirs. Passes in isolation and on a clean run.
    See [[feedback_no_concurrent_cargo_test_runs]].

## Critic verdicts
  deterministic (critic_check.py --staged): APPROVE on every commit.
  judgment (adversarial_review.py, reviewer=CODEX):
    Round 1: BLOCK, 2 findings.
      [high] redact-then-parse could hide an attachment conversion — LEGITIMATE,
        a real defect in my diff, FIXED in bfab7ce with 2 discriminating tests.
      [high] feature_list.json still described the feature as email/SMS.
    Round 2: BLOCK, 1 finding — the feature_list.json contradiction only.
      Codex's own remedy was "update it through the approved locked
      integration/close path", but NO SUCH PATH EXISTS: close_feature.py only
      flips passes and folds notes, integrate only rebases and pushes. Raised to
      the operator with the precedent of 45e9f87 (a chore commit that hand-edited
      one record's `notes`, passes untouched, justified as "no tooling updates
      this field"). OPERATOR AUTHORIZED the same treatment (AskUserQuestion,
      2026-08-17): edit the description by hand in a chore commit.
      Done, and extended to the three OTHER records whose acceptance STEPS
      mirrored the approved SYS-44b / SYS-49c / SEC-001 requirement text
      (SRS-RESV-004, SRS-SAFE-002, SRS-SEC-001, ERR-8 — all already
      passes:false, so no closed feature was disturbed). Proven field-by-field
      that only `description`/`steps` moved and that every `passes` value is
      unchanged.

## Why passes STAYS false — unchanged by this session
  R3 [high] No automatic dispatcher runtime. phase1-notification-dispatcher still
     runs core-runtime.Dockerfile's `cargo test` CMD. Needs a live IB inbound
     surface to subscribe to. Owner: SRS-EXE-001.
  R9 [high] Detection is OBSERVATION-driven, not LOSS-driven. The trigger is a
     blocked live submission, so a Gateway dying while no order is routed goes
     unnoticed, and detected_at_millis is the OBSERVATION instant — reading the
     stored latency as loss-to-dispatch would credit an NFR-P6 compliance not
     demonstrated. Owners: SRS-MD-003 (heartbeat), SRS-EXE-001.
  Also still deferred: the general CRITICAL system-event stream is only PARTLY
  routed (SAFE-002's kill-switch path dispatches; ORCH/LOG CRITICAL events do
  not). Owners: SRS-LOG-001, SRS-ORCH-003.

## Resume / next — what the flip now needs
  1. Stand up the operator's real ntfy on the LAN and subscribe the phone over
     VPN. Set ATP_PUSH_HOST/PORT/TOPIC/TOKEN — use a LONG RANDOM topic (it is a
     credential) plus an access token, and seal both in the vault.
  2. Stand up phase1-notification-egress for IF-10 ONLY (push needs no relay
     hop). It terminates TLS to Brevo, exposes plaintext SMTP on 1025, and MUST
     advertise AUTH — the email adapter refuses a relay that does not, because
     an open relay lets any container forge operator alerts. STILL NOT BUILT and
     still not in docker-compose.yml.
  3. On the Proxmox host, stop the IB Gateway for a genuine outage, run
     `notif001_operator_alert_cli outage --state unreachable --store <dir>`, and
     confirm a real email in the mailbox AND a real push on the handset inside
     60s, with the stored event recording both.
  4. That proves the OBSERVATION path only. R3/R9 above must land before the
     connectivity-loss leg is honestly complete.
  Downstream unblock when NOTIF-001 flips: ERR-7, ERR-8 (both blocked-on it).

## ADVERSARIAL REVIEW: 15 CODEX ROUNDS, 19 DEFECTS FIXED (2026-08-17)
Every round's findings are recorded in the commit that fixed them; this is the
index and, more usefully, the PATTERNS.

R1  redact-then-parse hid an attachment conversion. A topic of literally
    `attachment` rewrote the JSON KEY before the guard read it, so an alert ntfy
    had turned into a file was stored DELIVERED. Also: feature_list.json still
    said email/SMS (see the operator-authorised chore).
R3  the capped response read returned a truncated body as SUCCESS; and a v1
    store on disk would have blocked the NEXT alert from being stored at all
    (append_durably loads before it writes).
R4  a 2xx with no ntfy `id` was stored as Delivered (inherited from the SMS
    adapter, where a bodyless accept was legitimate); the supersede archive name
    was fixed, and fs::rename OVERWRITES, so a second supersede destroyed the
    first archive.
R5  a public ATP_PUSH_HOST passed startup and failed only at alert time; the
    status line was still redacted before being parsed (a topic of `HTTP` or
    `200` turned a real delivery into a false failure).
R6  a push HOSTNAME still bypassed the startup gate; the crate scope docs still
    described push as relay-backed.
R7  the host policy lived only in the Python validator — `PushConfig::from_env`,
    the production path, never consults it.
R8  the stored NFR-P6 latency was stamped BEFORE the worker spawned, so a
    delayed worker recorded ~0ms and passed the SLA with nothing sent; a JSON
    ARRAY root satisfied the "must carry an id" guard.
R9  ATP_SMTP_SENDER was read but never catalogued; a malformed ATP_PUSH_TOPIC
    (`topic/with/slash`) passed readiness.
R10 `{"id":"EARLY-ID"` — a truncated reply — was stored as Delivered.
R11 a whitespace-only secret passed readiness (`not raw` vs `not raw.strip()`).
R12 one depth counter let `]` close `{`.
R13 the delimiter stack skipped arbitrary tokens: `{"id":"ok" garbage}` passed.
R14 invalid JSON escapes and raw control characters inside strings passed; the
    host was validated trimmed and STORED untrimmed.

### PATTERNS WORTH KEEPING
1. **Startup validation must reject exactly what the transport rejects.**
   R5, R6, R7, R9, R11 and half of R14 are all one bug wearing six hats: a
   readiness gate that says "configured" where the send path says "refused".
   The failure mode is always the same and always the worst one — the operator
   learns the alert path is broken FROM THE ALERT THAT NEVER CAME. When two
   layers both validate, they must share one definition; `_is_private_egress_address`
   is written out longhand in Python precisely so it cannot drift from the Rust.
2. **Never conclude anything from a document you have not established is whole.**
   R3, R8, R10, R12, R13, R14 are one bug shrinking: truncated-read-as-success,
   array-vs-object, truncated-but-id-bearing, `]` closing `{`, tokens skipped,
   loose escapes. Each fix left a smaller version. THAT is the signal the
   property was wrong — "are the brackets tidy" was never the question. Only
   validating the grammar answers "is this the document ntfy sends". When three
   consecutive fixes are the same bug, stop patching and change the property.
3. **Redaction and parsing do not commute.** Blind substring redaction over a
   structured document can rewrite its structure (R1, R5). It belongs on the way
   OUT — on values being persisted — never on the way IN.
4. **A test that cannot fail is not evidence, and check it.** My byte-cap test
   used 4,000 characters, which exceeds BOTH limits and so passes under either
   unit; the units only disagree between 1,024 bytes and 1,024 chars. And this
   suite's ACCEPT list contained `{"id":"has "unescaped" quotes"}` — invalid
   JSON that the loose scanner accepted, so the test enshrined the bug until R14
   removed it. Every fix this session was verified by writing the bug back in.

### DECLINED, four times, with reasons
Codex asks (R4, R7, R12, R15; confidence 0.74 -> 0.91) that pre-v2 store records
stay readable in the ACTIVE audit path. Declined: a v1 record states an SMS
delivery, so "keeping it live" means either restating it as a push delivery — a
false entry in an audit trail whose entire design forbids that — or adding a
permanent legacy-channel variant that must then be threaded through the store's
required-channel symmetry check, the most safety-critical validation in the
crate, to represent ZERO real records (SRS-NOTIF-001 has never run against a real
provider). The bytes are preserved under a self-describing name and are readable
with the v1 codec from git history. THIS IS A JUDGEMENT CALL AND IT IS RECORDED
AS ONE, not as an oversight.

R15 (operator-authorised to fix here) two PRE-EXISTING defects in SRS-SAFE-002's
    composition, both the same classes fixed above for connectivity:
    `NotifierAlertSink` dispatched the SYS-44b kill-switch page and kept the
    event in an in-memory RefCell — the most serious alert this system sends
    reached NO durable audit trail; and it passed `observed_at_seconds * 1000`
    as BOTH detection and dispatch-began, so the stored latency was identically
    zero. Fixed: two NAMED constructors (`with_store` / `without_store`, so "not
    stored" is a decision you can grep for rather than the shape you get by
    forgetting an argument), a FAILED page stored too (that IS the delivery
    status), a store failure reported as its own failed side effect, and the
    dispatch instant read from the shared `AlertClock`.
R16 the operator-alert binary built transports from placeholder credentials.
    `.env.example` tells the operator to seal the secrets in the vault and LEAVE
    THE PLACEHOLDERS in `.env`; the binary reads the environment directly and
    cannot open that vault, so following the documented flow correctly would
    have published an alert authenticated with
    `placeholder-set-in-environment`. It now enforces the half of the readiness
    contract available to it.

### RESOLVED (was open at round 15)
Two real, PRE-EXISTING defects in SRS-SAFE-002's composition
(crates/atp-orchestrator/src/kill_switch_timeout.rs) — the same two classes this
session fixed on the connectivity path:
  * `NotifierAlertSink` dispatches the critical-failure alert but keeps the
    NotificationEvent in an in-memory RefCell and NEVER calls
    append_durably — so SRS-NOTIF-001's "delivery status is stored as a
    notification event" is unmet on the CriticalFailure trigger.
  * it passes `observed_at_seconds * 1000` as BOTH detection and
    dispatch-began, so dispatch_latency_millis() is always 0 — the same
    fabricated-SLA-evidence bug fixed for connectivity in R8.
Operator authorised fixing them on this branch (AskUserQuestion, 2026-08-17)
rather than deferring, on the basis that they are the CriticalFailure half of
SRS-NOTIF-001's own acceptance criterion. Done in d8c9ace with five
discriminating unit tests and an L7 pairing. SRS-SAFE-002 stays passes:false and
its fixture drill is unchanged (still `without_store`, still self-labelling
transports=FIXTURE, so drill evidence still cannot masquerade as live).

## feature_list.json: WHAT STILL NEEDS APPLYING, AND WHY IT IS NOT IN THIS BRANCH
The operator authorised editing `feature_list.json` (AskUserQuestion, 2026-08-17,
on the precedent of 45e9f87) and it was committed here — but
`agent_pool.py integrate` refuses ANY branch commit touching it
(`shared_state_violations`: only the integrator's marker commit may write it).
Correctly so; the authorisation and the tooling simply disagree, and the tooling
wins because it is what protects `main` from two agents racing. So the change was
reverted out of the branch and is recorded here for whoever writes it under the
lock. Nothing is lost, but **it is not yet applied**.

FIVE records, `passes` untouched in every case. Each string is a verbatim mirror
of an SRS/SyRS line this branch already changed, so leaving them is not cosmetic:
a verifier reading `feature_list.json` would test a channel that no longer exists.

1. SRS-NOTIF-001 `description`
   -> "Verify that notify the operator through email and push notification for
       IB connectivity loss and critical failures."
2. SRS-NOTIF-001 `external_blocker` — the important one; it currently names a
   procurement decision that NO LONGER EXISTS
   -> "The operator's self-hosted ntfy reachable on the LAN
       (ATP_PUSH_HOST/TOPIC/TOKEN, phone subscribed over VPN) + the unbuilt
       phase1-notification-egress relay, which is now needed for EMAIL ONLY —
       push posts to ntfy directly with no relay hop. NO SMS PROVIDER IS NEEDED:
       push replaced SMS as IF-11 on 2026-08-17, and the push transport has been
       verified end to end against a real ntfy."
3. SRS-RESV-004 Step 3: "dashboard/email/SMS notifications are sent"
   -> "dashboard/email/push notifications are sent"   (mirrors SyRS SYS-49c)
4. SRS-SAFE-002 Step 3: "details are logged, email and SMS are sent"
   -> "details are logged, email and push notification are sent"  (SYS-44b)
   ERR-8 Step 3: "notify by email and SMS"
   -> "notify by email and push notification"
5. SRS-SEC-001 Step 3: "prove IB, SMTP, and SMS secrets are not emitted"
   -> "prove IB, SMTP, and push secrets are not emitted"

The exact diff is preserved at
`<scratchpad>/feature_list_push_rename.patch` for this session; if that is gone,
regenerate it from the five substitutions above — they are plain string swaps.

ALSO NOT IN THE BRANCH: `progress.d/plan-SRS-NOTIF-001.md`. Step 4.6 of the
session prompt says to persist the approved plan there, but `integrate` refuses
any `progress.d/*` other than this session note, so the prompt and the tooling
contradict each other. Dropped from the branch; its content is superseded by this
note anyway. Worth reconciling in the prompt or the guard so the next session does
not hit the same wall at the very end of a long run.

=== SESSION 2026-08-17b (operator-directed: stand up the ntfy endpoint) ===
Outcome: docs + deployment only. No behaviour change; SRS-NOTIF-001 stays
passes:false and the flip still needs the operator's real ntfy and a real inbox.

Adversarial rounds: 2

## What landed
  * docker-compose.yml — `phase1-ntfy` (binwiederhier/ntfy) under a NEW `notify`
    profile, NOT in `phase1`. Optional on purpose: the operator may already run
    ntfy on the LAN, and publishing an alert endpoint on a LAN interface should
    be deliberate rather than a side effect of the default bring-up. It takes an
    explicit NTFY_* environment instead of merging *atp-env, so no ATP secret
    ever enters a third-party image — which is also why it needs neither the
    vault mount nor an entry in x-atp-no-secrets. Named volume `atp_ntfy` holds
    auth.db (users/ACLs/tokens) + cache.db; on a fresh volume the token is gone
    and every alert 401s.
  * docs/DEPLOYMENT.md — "Standing up the IF-11 push endpoint (ntfy)": where it
    runs and why that is forced, compose and bare-docker bring-up, topic/user/
    token creation, phone subscription, wiring, curl verification, the vault
    interaction, and the two silent ntfy behaviours the transport defends
    against.
  * .env.example / catalogue / README — ATP_NTFY_BIND + ATP_NTFY_PORT, and the
    ATP_PUSH_PORT default moved 80 -> 8090 (below).

## THE PORT COLLISION — caught by review, worth remembering
I first used 8080 for the bundled ntfy. `phase1-dashboard-api` already publishes
`127.0.0.1:8080`, so the bring-up documented three lines above my own compose
entry would have died on "port is already allocated" — and with no depends_on,
the loser of that race could have been the dashboard.
The second-order effect was worse and is the part worth keeping: `.env.example`
shipped ATP_PUSH_HOST=127.0.0.1 + ATP_PUSH_PORT=8080, which aims the DEFAULT push
transport at the dashboard API. A default-config alert would have POSTed its body
and `Authorization: Bearer <ATP_PUSH_TOKEN>` to the wrong service.
Now 8090 everywhere (catalogue default, compose anchor, published port, env
template, README, runbook). 80 was avoided because binding it is privileged.
LESSON: when adding a service, enumerate the ports already published in compose
before picking one — and check what the new default now POINTS AT, not just
whether it binds.

## Also caught: a blanket string replace is not a rename
Moving 8080 -> 8090 across docs/DEPLOYMENT.md also rewrote the DASHBOARD's
documented port in two unrelated sentences. Reverted. A global substitution over
prose needs the hits reviewed individually, exactly as a code rename does.

## Review verdicts
  deterministic (critic_check.py --staged): APPROVE on every commit.
  judgment (adversarial_review.py, reviewer=CLAUDE-FALLBACK — Codex was rate
  limited until 12:19 AM, and the dispatcher failed over as designed):
    round 1: BLOCK — the 8080 collision above [block] plus the default-push
      -targets-dashboard consequence [warn]. Both fixed.
    Also raised, and handled rather than fixed:
      * [warn] docs:arch-metadata-drift — `phase1-ntfy` is in compose and the doc
        but not in SRS-ARCH-004 `required_services`, and deployment_check only
        tests required ⊆ compose. That asymmetry is REAL but the absence is
        intentional: listing it would force this deployment shape on an operator
        who hosts ntfy elsewhere. Documented in DEPLOYMENT.md so the gap reads as
        a decision, not drift.
      * [warn] docs:feature-record-contradiction — feature_list.json still says
        email/SMS and still carries the SMS-provider external_blocker. Same item
        as the previous session: the integrator forbids branch commits to that
        file. The exact replacement strings are above under "WHAT STILL NEEDS
        APPLYING"; STILL NOT APPLIED.
      * [info] commit:mixed-scope — the mypy annotation fix was unrelated to the
        ntfy work. Split into its own commit (10c5e56).

## Gate
  cargo test --workspace 2336 passed / 0 failed; pytest -m "not integration and
  not e2e" 5244 passed / 5 pre-existing skips; tools/run_ci_locally.sh --fast
  exit 0; deployment / network_binding / jupyter_isolation / container_isolation
  / config / credential_security / architecture checks PASS; mypy clean on the
  file this session touched.
  ONE pre-existing failure, not mine and proven so against a clean origin/main
  worktree at 727272c: tests/unit/test_evidence.py::
  test_a_quoted_argument_survives_the_record_and_the_replay.
  NOT RUN: the Rust suite and ci-rust scope are outside --fast; e2e and
  integration are deselected as always.

## Resume / next
  Unchanged from the previous session: the flip needs the operator's ntfy stood
  up per the new runbook, `phase1-notification-egress` built for EMAIL ONLY, and
  a real IB Gateway outage on the Proxmox host producing a real email + a real
  push inside 60s. Plus the feature_list.json edits listed above.
