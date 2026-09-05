# Safety paths — kill switch, connectivity, stale data, live mode, orders

`SAFETY_PATH_RE` in `tools/critic_check.py` decides what counts. If your diff matches, the
SAME staged commit must contain a `tests/domain/` test or the deterministic critic blocks —
including when only the *filename* matches (a notes-only chore for a safety-named feature).

## Validate everywhere, fail closed

1. **Validate at EVERY entry point, not the outermost.** EXE-003 was blocked three rounds
   running for the same reason: the adapter validated, then `dispatch_order` did not, then
   `route_order` / `submit_live_order` sent straight to the broker unvalidated. Enumerate
   the arms and gate all of them. `(EXE-003 r3/r4)`
2. **Precedence matters.** Put the live-path `validate()` in the connected-and-fresh arm
   AFTER the connectivity and freshness gates, or malformed orders on the disconnected/stale
   paths start returning your new error instead of the expected one. `(EXE-003)`
3. **Validation PARITY with the sibling intake.** If the simulation's `validate_leg` checks
   non-blank symbol + quantity > 0 + price positivity, the live envelope must check the same
   set. Prices-only is a fail-open. `(EXE-003 r2)`
4. **A new broker-touching seam needs an execution-layer gate, not just an adapter one** —
   mirror the existing non-live → connectivity → freshness → validate → broker precedence.
   `(EXE-004)`
5. **Keep the authority inside the engine.** A caller-supplied, publicly-constructible,
   `Clone`-able designation lets a strategy pass its own authority or keep a stale clone past
   `demote`. Private field, no `Clone`, and the gated entry takes no authority parameter.
   `(EXE-001)`
6. **The gate's inner method must be `pub(crate)`** so a caller cannot self-assert
   `StrategyMode::Live` and bypass designation. `(EXE-004)`
7. **Fail closed on out-of-scope-but-well-formed input.** A one-leg `MultiLeg`, or an
   equity leg in an options composite, is structurally valid and outside the SRS scope —
   reject it with a distinct error. `(SIM-001)`
8. **Options fail closed until contract identity exists.** An `AssetClass::Option` order
   carrying only an underlying conflates distinct contracts. Freshness must key on the FULL
   contract (`canonical_key`), never the underlying. `(EXE-003 r6; EXE-004)`
9. **Do not add an `OrderErrorCategory` variant for a one-off** — it is a 111-reference
   taxonomy pinned by many check tools. Reuse the bucket and carry the precise reason in
   `error_type`. `(EXE-003)`

## Evidence and disposition

10. **Cross-validate disposition against evidence in BOTH directions.** A "nothing ran"
    disposition whose payload shows cleanup ran → refuse. A "sequence ran" disposition whose
    legs are NOT_ATTEMPTED → refuse. A SUCCEEDED leg without its own evidence counter →
    refuse. `(SAFE-002 r1)`
11. **Validate the full nested identity** (order id, symbol, side, positive-int quantity) —
    the record must never log `None`. `(SAFE-002)`
12. **Every port is fallible, including the read/confirmation port.** A fill-confirmation
    probe is an IB-touching boundary; on probe failure the gate must fail closed WITHOUT
    pretending success and WITHOUT firing destructive cleanup on an unconfirmable state —
    with a DISTINCT error category from a timeout. `(ERR-8)`
13. **If an error points at a separately-persisted record for facts the consumer needs, ask
    what happens when that persistence fails** — and duplicate the recovery-critical facts
    onto the error itself, with a flag saying whether the durable record landed.
    `(ERR-8; SAFE-002 r4)`
14. **Destructive broker actions before notification dispatch** (cancel → disconnect →
    notify), pinned by a shared-log cross-port ordering test. `(SAFE-002 r7)`
15. **Fixture-tier quarantine.** The runtime self-labels its transport tier
    (`transports=FIXTURE`); unknown tier → refuse; durable logging of drill evidence needs
    an explicit opt-in; the label travels into the record. `(SAFE-002 r6)`
16. **Total C0 escaping in any hand-rolled JSON emitter** (`c < 0x20 → \u00XX`, not just the
    five obvious ones). Corruption AFTER the side effects ran suppresses the audit record.
    `(SAFE-002 r3; DATA-020)`
17. **Typed launch failures** — wrap `OSError`/`PermissionError` from a subprocess launch,
    not just `TimeoutExpired`. No untyped path at an operator boundary. `(SAFE-002 r5)`

## Alerting

18. **A CriticalFailure is NEVER suppressed.** Restart-window suppression silences
    connectivity loss only, and suppression is all-required-channels-or-none. `(NOTIF-001)`
19. **Enforce the required channel set in the core**, not by caller convention — each
    required channel exactly once, missing/duplicate is a typed error before any send.
    `(NOTIF-001 r1)`
20. **Silence bugs are the alert-path equivalent of a false green.** A *suppressed* record
    must not arm the cool-down a *real* outage would page through — separate windows per
    class, and decide suppression before consulting any cool-down. Arm the cool-down on the
    attempt, but only once the worker is actually running. Coalescing must carry the folded
    count into the next alert. A forgeable `scheduled_restart` boolean must agree with the
    authoritative state, and disagreement pages. `(NOTIF-001)`
21. **Every relay-controlled string that reaches a PERSISTED field is scrubbed at the point
    of construction — success paths included.** This took four rounds because the fix was
    applied per instance: the SMTP reply, the SMS body, the SMS status line, then the SMTP
    success receipt. A stored receipt is as durable as a stored error, and a private network
    does not make a peer trusted. `(NOTIF-001)`
22. **Link-local is not private.** `169.254.169.254` is the cloud metadata endpoint and an
    adapter usually sends its credential right after connecting. Allow loopback + RFC1918
    only, unwrap IPv4-mapped IPv6, and validate EVERY resolved address — a
    `[private, public]` round-robin passes on the first. `(NOTIF-001)`
23. **Reporting a problem must not extend it.** A sink running inline inside
   `submit_live_order` puts two channel deadlines between detection and reconnect. Move the
   I/O off the caller's thread; on spawn failure record the miss rather than falling back to
   inline — that fallback fires exactly under resource exhaustion. `(NOTIF-001)`
24. **A delivery status must be unforgeable by construction** — opaque record with a
   `pub(crate)` constructor, mintable only by the dispatcher from a real send. `(NOTIF-001)`
25. **An alert sink is FALLIBLE.** A missed operator page is itself a safety event: surface
   `alert_failures` and continue to safety — one bad channel must not suppress the others.
   `(DATA-019)`
26. **`atp-execution` must NOT depend on `atp-notification`, not even as a dev-dependency**
   (`tools/dependency_boundary_check.py`; it cascades 8 architecture-test failures). Emit
   through a neutral port; bind the real notifier at the composition root. `(DATA-019)`

## Corporate actions against live state

27. **"Resting live orders" is not "non-terminal".** Only `Acked` has a known full working
    quantity → adjust. Everything else that could still rest or fill at the stale basis
    CANCELS fail-closed — `PartiallyFilled` (remaining qty unknown at this layer), `New` and
    `PendingSubmit` (pre-ack race). Enumerate all nine states. `(DATA-019)`
28. **Prices round; quantities are EXACT.** A fractional reverse split is cash-in-lieu —
    cancel, never truncate. Adjusted price ≤ 0 or overflow → cancel. Adjust = cancel-then-new
    via `cancel_replace`, never in-place mutation. `(DATA-019)`
29. **Dividends are ADDITIVE on a position's cost basis**, not multiplicative — the
    price-ratio factor leaks ~2.5% of absolute P&L per ex-date. Positions need no rounding at
    all, and signed quantity/basis must survive (a short reverse-split yields a negative
    quantity). `(DATA-020)`
30. **Match symbols canonically** (`trim().to_uppercase()`) — `OrderSubmission::validate`
    only rejects blanks, so raw equality misses `aapl`. `(DATA-019)`

## Callback authority and durable submit seams

31. **`#[non_exhaustive]` on an enum does NOT stop a downstream crate constructing its unit
    variants** — empirically confirmed. To make "a callback can only come from the
    authority", use an opaque newtype over a PRIVATE inner enum (proven non-constructible,
    E0603). `(SDK-004)`
32. **Bind derivation to the state-owner's mutation.** A free `for_transition(from, to)` over
    caller-supplied states lets a dispatcher fabricate `Acked→Filled` for an order in another
    state. Keep it `pub(crate)`; the only public path is the ledger transition that feeds the
    tracked order's REAL `from` and returns a category only on a successful mutation.
    `(SDK-004)`
33. **Return ALL cascade events, not one.** A transition driving an original to a
    non-cancelled terminal auto-rejects a held replacement, which is then terminal — its
    REJECTED callback could never be re-derived, so omitting it silently loses a strategy
    callback. `(SDK-004)`
34. **Durable submit: stage on a CLONE, persist, then swap.** Mutating the caller's outbox
    before the persist poisons it on a failed write, and a stale PENDING_SUBMIT may
    resubmit. `(EXE-009)`
35. **Validate BEFORE any durable write.** One persisted invalid order bricks the whole
    outbox on restart, because a fail-closed `deserialize` re-validates on restore.
    `(EXE-009)`
36. **Split persistence failures by whether a live order EXISTS**: pre-broker (safe to
    retry) vs ack-not-durable (the broker accepted — carry the receipt, NEVER blind-retry,
    reconcile) vs rejection-cleanup-failed. One `Persistence` variant conflates safe-retry
    with duplicating a live order. `(EXE-009)`
37. **Rejection cleanup must be durable, not best-effort** — `let _ = observe_state(...)`
    leaves a resubmittable PENDING_SUBMIT. `(EXE-009)`
38. **Reconcile by GROUPING broker rows by key**, never `collect()` into a map: duplicate
    rows for one correlation key are the duplicate-live-order hazard, so surface them
    unresolved. `(EXE-009)`
39. **Prove the ordering.** A broker stub that asserts the snapshot file EXISTS on disk at
    `submit_order` time is what proves durable-before-submit. `(EXE-009)`

## Durable safety blocks

40. **Persist the block BEFORE the destructive side effects it describes, then amend it.** The
    demotion gate cancelled the unfilled order, paged the operator, and only then engaged the
    demotion-pending lockout — so the record could carry their outcomes. A process that died in
    between left no lockout at all, and the next one read the empty store as "nothing is
    pending" and could promote. Engage first with the outcomes `NotAttempted` (the block is live
    from that instant), run the side effects, then `amend`. A crash after phase one leaves a
    record that UNDERSTATES what was attempted — the safe direction to be wrong in — and the
    amend path must refuse to create a lockout, or it is a second engage wearing a disguise.
    `(SRS-RESV-004 r4)`
41. **A failed durable write must leave a fail-closed STATE, not just a truthful error.**
    Returning `promotion_block_is_durable = false` described the hole precisely and closed
    nothing: the store was still empty, so the next attempt read `Clear` and promoted. The port
    impl now poisons itself on a failed engage and reports the blocking state until an operator
    resolves. Two counter-rules learned the same day: poison ONLY on the failures that mean
    "the block is absent" — an `AlreadyPending` refusal means the block EXISTS, and poisoning
    there outlives the operator's resolution and wedges the swap path permanently; and state the
    residual (an in-memory poison does not survive a restart) instead of implying it does.
    `(SRS-RESV-004 r3)`
42. **An action keyed on a caller-supplied identity that selects ACCOUNT-level state must prove
    that identity against the authority, before the first port call.** `execute_demotion_sequence`
    liquidated every position in `LiveExecutionState::open_positions` — the whole account book —
    using `request.demoting_strategy_id`, without checking it against the live registry. The id
    was not a label on the audit record; it decided whose positions were flattened. Prove it
    first, and refuse on all three failures: nobody live, the wrong one live, and MORE than one
    live (an already-broken single-live invariant must not be resolved by guessing).
    `(SRS-RESV-004 r1)`

## Guard placement and default direction

43. **A guard that runs AFTER a gate with destructive side effects is too late.** The
    live-slot revalidation sat inside the promotion gate, which runs after
    `resolve_demotion` — and `resolve_demotion` is not read-only: on its timeout branch it
    engages the durable lockout, cancels unfilled liquidation orders, and pages the operator
    on three channels. A swap queued behind another one, arriving with a demoting id that
    was no longer live, fired all of that against the wrong strategy and only then was
    refused. Ask of every guard: what has already happened by the time it runs?
    `(RESV-005 r7)`
44. **Write safety predicates as ALLOWLISTS, never denylists.** `flat_confirmed()` was
    "everything except `DemotionRefused`". Two new refusal variants — added one round earlier
    to guard the live slot before the demotion runs — silently inherited `true`, so the CLI
    printed `demotion-outcome:FLAT_CONFIRMED` and the REST body reported
    `demotion_state: DEMOTED` for a swap in which no demotion had happened at all. A denylist
    defaults a NEW case to the dangerous answer; an allowlist defaults it to an under-claim.
    Pin BOTH directions, or "fix" it by returning false everywhere. `(RESV-005 r9, a defect
    introduced by r7's own fix)`
45. **Declaring the fixture TIER is not stating the fixture FACTS.** An opt-in flag
    (`--allow-fixture-safety-inputs`) made the tier explicit while the values still defaulted
    to a successful demotion, a flat account and a dummy artifact hash — so the flag alone
    promoted on facts nobody stated, which is the same silent success the opt-in existed to
    stop, one layer in. Every fixture value standing in for a SAFETY fact must be individually
    required, and the check must reject an `unwrap_or(` in its parser. `(RESV-005 r1/r3)`
46. **An audit record must not be appended before the durable state it describes is
    published.** The gate records the promotion event while the designation is still in
    memory; the caller persisted it afterwards. A publish that failed before its rename left a
    journal claiming `promoted:true` for a state change the authority never accepted, and
    recovery tooling reconciles from that. Buffer the event and commit it once the state is
    settled — and keep journalling REFUSALS, or deferring the append quietly becomes
    "record only successes". `(RESV-005 r6)`

## Guarantees have to be placed where they are still available (SRS-RESV-006)

47. **For an IRREVERSIBLE action, "reported loudly" is not "enforced".** RESV-006 shipped a
    swap that recorded its cool-down window afterwards, and when that write failed it
    returned success carrying `NotStarted`, a non-zero exit and an explicit page. All true,
    and all useless: the designation had moved, the book was flat, and the automatic triggers
    the window existed to suppress were armed against the strategy just promoted. Rolling
    back would have been worse. The only point at which the requirement was still guaranteeable
    was BEFORE the swap — so the writability of the durable state is now PROVED first
    (a real locked read-modify-write through the same publish path, not a permissions guess)
    and the swap refused while nothing has mutated. Ask of every post-action durable write:
    *if this fails, what can I still do?* If the answer is "describe it", move the check
    earlier. Keep the loud reporting for the residual race, and state that residual.
    `(RESV-006 r4)`
48. **Order an UNWAIVABLE refusal ahead of a waivable one.** Both were pre-side-effect, so
    neither was less safe — but an operator inside a cool-down whose store was *also* broken
    was told to acknowledge, acknowledged, and only then hit the wall no acknowledgement
    could move. Same reason a corrupt window now reports UNRECORDABLE rather than
    CONFIRMATION_REQUIRED: confirming cannot repair a file, and the old code sent them in a
    circle. `(RESV-006 r4)`
49. **The instant an operation STARTS is not the instant it COMPLETES, and a safety window
    keys on the second.** One `observed_at_seconds` was read at CLI entry and reused both to
    classify the existing window and to stamp the new one. A demotion may legitimately run
    for the whole SYS-49b liquidation timeout, so a seven-day window opened up to 60s early
    and the automatic triggers resumed that much sooner — the one direction a cool-down must
    never move. Read the completion instant *after* the thing completes, make it fallible,
    and never let it fall back to the start instant: that is the bug wearing a default.
    `(RESV-006 r5)`

50. **A caller-supplied proof is a forgeable proof — and a static check over YOUR call
    sites is not a property of the API.** SRS-RESV-006 passed the gate a `&CooldownState`
    and documented the fabrication risk as "closed by a static check, not by types". The
    check really did pin both CLIs to the resolver, and it was still wrong: `CooldownState`
    is a public enum, so any external caller could hand `NeverSwapped` to the execution gate
    and swap straight through an active window. Give the gate the PORT and let it read the
    fact itself. That also closes a staleness gap the value form hides — the state is then
    read at the instant the decision is made, not whenever the caller happened to look.
    Keep the caller supplying only what the store cannot know (here: whether a human was
    shown the warning and said yes). `(RESV-006 r10)`

## A gate on the order path is inside the order's latency budget (SRS-MD-005)

52. **A port the execution engine consults INLINE is spent inside NFR-P1, not
    beside it.** SRS-MD-005's connectivity producer probed the gateway with a
    2-second TCP deadline on every `state()` call, and `submit_live_order`
    consults that port on every live submission — so against a black-holing
    endpoint (a paused Gateway VM, a DROP rule, or the gateway mid-restart
    holding the socket unaccepted, which are precisely the conditions the
    feature exists for) every order stalled ~2 s before reaching the broker,
    against NFR-P1's 1,000 ms p95 for the whole signal-to-acknowledgement path.
    The constant had been argued only against NFR-R2's 15-second RECONNECT
    budget — a real requirement, and the wrong one. Ask which budget your
    deadline is actually spent in, bound it to a fraction of that, and assert it
    against the requirement rather than against feel. `(SRS-MD-005 r5)`
53. **Cache the sampled FACT, never the derived STATE.** The same fix added a
    short-TTL cache so a burst of orders costs one probe. Only reachability is
    cached; the phase is recomputed from the clock every read, because the two
    instants that matter — the start of the suspension and the end of the
    window — are exactly where a cached verdict would be wrong. State the
    residual (a gateway that returns can read stale for up to the TTL) and pin
    that a backwards clock step reads as "inside the TTL" rather than "expired",
    or a clock correction stampedes a gateway that serves one API client.
    `(SRS-MD-005 r5)`
54. **A refusal must say WHICH refusal it is.** One boolean gate returned the
    same "suspended for the scheduled restart" error before and after the
    window, so a genuine outage was reported to the operator as planned
    maintenance — telling them to wait out an incident. The gate now returns a
    reason, not a bool. Wherever two refusals share a category, the distinction
    has to survive to the surface the operator reads. `(SRS-MD-005 r2)`
55. **A configured knob that changes no behaviour is a lie with a fuse.** Three
    keys were catalogued, documented in `.env.example` and the config README as
    controlling the suspension window, and validated on startup — while the
    binary read compiled-in constants. Everything passed; the discovery would
    have been a restart. After adding a config key, prove it MOVES a verdict at
    a fixed instant, and refuse a present-but-empty value rather than defaulting
    (an empty variable is usually one that expanded to nothing).
    `(SRS-MD-005 r4)`

56. **A refusal that tells the operator to RETRY must be reachable only by
    requests that could succeed on retry.** SRS-MD-005's subscription gate
    consulted the restart window before canonicalizing, so during the window an
    option subscription, an empty symbol or an empty strategy id came back as
    "planned maintenance, retry once the window closes" — sending someone to
    wait five minutes for something that can never succeed. Rule 2's precedence
    advice (validate AFTER the connectivity gate) is right when the blocked-path
    error is the one the caller expects; it inverts the moment your refusal
    carries an instruction. Ask what the message TELLS them to do, and put the
    checks that can falsify it first. `(SRS-MD-005 r12)`
57. **A stale fact wearing a fresh label is this feature's whole defect class.**
    Five separate findings across twelve rounds were one shape: evidence that
    re-derived a flag instead of reading it, a cached `Connected` outlasting the
    gateway, an accessor promising a TTL it never enforced, a proof line naming
    a phase it had not entered, and a config key documented as controlling
    something it did not reach. Whenever a value is cached, derived, or
    described, ask what makes the description true AT THE MOMENT IT IS READ —
    and if nothing does, either enforce it or stop claiming it. `(SRS-MD-005)`

## Deterministic-critic false positives (reword, don't disable)

- `money:float-arithmetic` fires on the substring `price/quantity` in a comment (the `/`
  reads as division) and on `price-` hyphenations. Reword to "price and quantity" /
  "adjusted price". `(DATA-019)`
- It also fires on `price * factor` in prose. Say "a value scaled by a split ratio".
- `tests:skip-without-reason` fires on the literal decorator token in PROSE — a playbook rule
  or a session note that quotes it is flagged even though it contains no test. Write "a pytest
  skip decorator" instead of pasting the token. `(RESV-005, on its own write-back)`
  `(DATA-012)`
50. **A waiver needs proof of WHO is waiving.** If a rule exempts one class of caller
    ("manual swaps may proceed with a confirmation") and the exemption is expressed as a
    standalone flag, every caller can claim it — including the class the rule exists to
    stop. Look at what actually reaches the gate: RESV-006 passed a bare
    `ManualCooldownAcknowledgement`, and the request carried no trigger kind, so an
    automatic proposal converted into a request and handed `Acknowledged` executed inside
    the window it was meant to be ignored in. Make the waiver a FIELD of the exempt
    variant, so the forbidden combination has no representation, and put the two rules in
    one predicate so they cannot drift. Then say plainly what it does not achieve: a caller
    can still misdescribe itself, and no type inside one process prevents that.
    `(RESV-006 r17)`
51. **The non-vacuity control for a suppression rule is the case that still fires.** Tests
    that only assert "refused during the window" pass on a gate that refuses always — which
    silently disables the feature the suppression protects. Pair every suppression case with
    the same caller succeeding outside the window, and with the exempt caller succeeding
    inside it. `(RESV-006 r17)`

59. **Caching only the negative outcome trades a latency defect for a churn defect.** The
    reachability probe cached `Unreachable` (expensive) and re-probed on every `Reachable`,
    reasoning that a successful connect returns in microseconds. True, and irrelevant: the
    gate is consulted once per order, so a healthy order stream opened and dropped one TCP
    connection per order against the gateway the same module elsewhere calls a scarce
    resource. Cache both, with ASYMMETRIC TTLs - a stale `Connected` is the dangerous
    direction, so it expires in 100 ms while a stale `Unreachable` may live 1 s. The bound
    is now the safety property, so pin it at COMPILE time (`const _: () = assert!(...)`)
    and test both directions: reuse inside the window, re-probe one nanosecond past it.
    `(SRS-MD-005 r14)`

60. **When you change a cache's policy, grep every surface that FILTERS on the old TTL.**
    Adding a 100 ms positive TTL to the probe cache left `last_outcome()` - the retained-
    fact accessor - still filtering on the 1 s negative TTL, so
    a `Reachable` could escape for ten times the bound the module had just installed as
    its safety property. Extract the decision into one `ttl_for(outcome, configured)` and
    route both call sites through it: two copies of a bound drift within a single round.
    And re-read the rustdoc of every method you touched - `state()` still promised "a
    fresh probe on every read" on the trait method the order gate calls.
    Corrected later: an earlier version of this entry called `last_outcome()` "the method
    that reports reachability to an operator". It has no production caller at all - the
    CLI reads `observe_if_needed()`. The BOUND still had to agree across both, which is
    the rule; the surface was overstated, which is the kind of small false claim these
    entries exist to stop. `(SRS-MD-005 r21)`
    `(SRS-MD-005 r15)`
