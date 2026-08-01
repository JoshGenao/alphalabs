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
