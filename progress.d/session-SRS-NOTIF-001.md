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
