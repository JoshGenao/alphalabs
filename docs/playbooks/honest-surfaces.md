# Honest surfaces — REST, CLI, WebSocket, dashboard panes

The failure mode of an operator surface is not a crash. It is a green light over a fact
nobody verified. Every rule here is one review round somebody already paid for.

## The payload

1. **Unknown is `null`, never `[]`.** `ok:true, alerts:[]` is all-clear-shaped to every
   consumer, no matter what a separate deferred cell says. `(UI-1 r1)`
2. **Tri-state every headline fact.** `activated` / `recovered` / `completed` must be
   true / false / **null**. `false` only when the source is readable AND genuinely empty;
   unreadable, corrupt, or unconfigured → `null`. Both directions are lies. `(UI-4 r2)`
3. **Separate absent from present-but-unreadable.** They need distinct error types
   (`LOGS_STORE_MISSING` vs `LOGS_STORE_CORRUPT`) and distinct rendering. `(LOG-001 r7)`
4. **Refuse undeclared parameters (400), never silently drop them.** Accepting and ignoring
   `?limit=10` reports a server-capped page as if the caller's bound was honoured.
   `(LOG-001 §3)`
5. **A request for something that cannot exist is a 400, not an empty result.**
   `?log_class=strategy&source=kill_switch` answered with `[]` reads as "no kill-switch
   activations". `(LOG-001 §1)`
6. **Report `returned` / `matched` / `truncated`, and say when a count is unavailable.** A
   page-only read must publish `matched: null` + `page_only: true` and render "newest N",
   never invent a total. `(LOG-001 r24)`
7. **State the scope of what you verified.** A tail read validates only the window it
   scanned, so `ok:true` overclaims. Publish `integrity_scope: "page"` and define `ok` as
   "the read succeeded", not "this trail is healthy". `(LOG-001 r32)`
8. **Carry the identity a consumer needs.** A pane where every one of 30 strategies renders
   an indistinguishable line is a functional loss, not a cosmetic one. `(LOG-001 r12)`
9. **State coverage; do not imply it.** A pane showing three kinds of event reads as "the
   system emits three kinds of event". Give every declared source/event type a
   produced/partial/deferred verdict, name the owner of each gap, and DERIVE the verdict
   from the produced set so it cannot drift into a second copy. `(LOG-001 §8, r37)`

9b. **Never derive a headline state as "X, or else Y".** `demotion_state` was
   `FLAT_CONFIRMED or else DEMOTION_PENDING`, and only the *promotion* field was checked
   against a closed vocabulary. A stale or truncated producer whose output carried
   `promotion:PROMOTED` but no readable `demotion-outcome` therefore returned a 200 reading
   "promoted, demotion pending" — a live promotion reported with no successful-demotion proof
   behind it. Require the field, check it against the closed set, AND refuse incoherent
   COMBINATIONS (`PROMOTED` + not-flat is a contradiction, not a state). `(RESV-005 r8)`
9c. **A non-2xx is a promise that nothing mutated** — check what your consumer already
   believes before choosing a status. `assets/app.js` says so in a comment ("a refusal
   (non-2xx) mutated nothing — retry is allowed"), which makes an executed-but-BLOCKED swap a
   200 carrying its outcome, and makes a post-rename persistence failure a 200 too: the
   atomic rename already moved the live slot, so answering non-2xx would invite a retry over
   changed state. Split persistence outcomes by whether the durable state ALREADY changed.
   `(RESV-005 r5)`

## The surface itself

10. **An uncovered capability gets NO public surface.** A flag that always errors is still
    a surface; remove it from the declaration so the arg parser refuses it before dispatch.
    "No public surface exposes X" includes the Rust crate API — a `pub use` re-export
    counts. `(LOG-001 r6/r11; DATA-012 r13)`
11. **Readiness must never overstate.** A workflow is `fully_served` only when EVERY
    operation — REST *and* CLI *and* the WS publisher obligation — is wired: `real == total`,
    not `real > 0`. A readiness CLI must exit non-zero when the body says `ready:false`.
    `(API-001)`
12. **Claim readiness from delivery, not from wiring.** Claim the channel on the first poll
    that reads cleanly, not in `start()`; make the claim revocable, because readiness that
    cannot be revoked eventually lies. See
    [lifecycle-and-concurrency.md](lifecycle-and-concurrency.md). `(LOG-001 r10/r15)`
13. **Confirmation guards precede dispatch on every surface** — route-level *and*
    action-level (a shared route whose `rollback` action needs confirm), REST and CLI. Prove
    it with a spy handler that is never reached. `(API-001)`
14. **Exit codes must be declared honestly.** A live command whose handler can 400/500 must
    declare `USAGE_ERROR` and `INTERNAL_ERROR`; a manual promising automation a surface that
    cannot fail is a wrong contract. `(LOG-001 r22)`
15. **The advertised entrypoint must actually work.** Subprocess-test it. `(API-001)`

## CLI input

16. **Allowlist arg parser, one pass — never scan-for-known-flags.** A typo (`--sorce`) is
    silently dropped, the CLI falls back to a default the operator never chose, and reports
    success. Reject unknown, duplicate, value-less flags, and a value that is itself a flag.
    `(BT-001)`
17. **Reject degenerate numerics at the boundary** (`--cash 0`, `-1`), before building the
    request. `(BT-001)`
18. **Canonicalize inputs through the SAME path the consumer uses** (e.g.
    `SecurityKey::new` → trim + upper), or `--symbol aapl` falsely reports "not available"
    against canonically-stored records. `(DATA-005 r5)`

## Panes over a live or deferred producer

19. **Every degraded branch fails closed AND clears prior state** — 5xx, fetch exception,
    stalled fetch (`AbortSignal.timeout`), 404 route-disappearance, malformed payload. Not
    just the caption: the rows, the beacon, and the dot. `(UI-1 r3)`
20. **Fail-closed field parsing.** Contract fields arrive as strings; truthiness reads
    `"false"` as acknowledged. Only an explicit true acknowledges; unknown = ACTIVE.
    `(UI-1 r4)`
21. **Freshness of the poll is not health of the producer.** While the producer is deferred,
    drive the dot from the render function (wait/deferred), not the shared freshness
    monitor. `(UI-1 r2)`
22. **Two feeds for one buffer will race.** A REST poll that *started* before a live event
    can resolve after it and erase it. Merge, don't replace; key the merge on a real record
    id (values repeat by design); sort by timestamp because held-back events are not always
    newer. `(LOG-001 r13/r16/r38)`
23. **Pin it with route-interception e2es** (`page.route` fulfilling fake payloads) — the
    real-feed branch is testable before the producer exists. `(UI-1 r6)`

## Mutating controls (a button that changes live state)

24. **Stale or unknown truth must never be left ACTIONABLE.** Reconcile every burst
    (generation stamps + sweep) so a removed entity loses its control; disarm any staged
    confirmation first. `(UI-2 r1)`
25. **Bind success to a confirmed id read back from durable state, never to the POST.**
    A per-call 2xx is not the end state. `(UI-5 r2/r9)`
26. **Keep burst state PER SOURCE** (WS vs REST poll) with one global monotonic generation
    for sweeping — shared counters make normal interleaving read as corruption and clear a
    healthy table. `(UI-2 r3)`
27. **Any unknown safety field blocks the control.** Enable only with the full safety
    picture resolved and clear. `(UI-5 r5–r12)`
28. **Timeouts must exceed the operation they wait on** — a 30s fetch timeout over a 60s
    demotion manufactures an ambiguous result. `(UI-5 r11)`

## Evidence panes (reporting what a past run did)

29. **A readable record is not a valid record.** `{}` parses. Require the record to
    substantiate the claim, with three-way identity agreement (record id == report id ==
    response id). Fail to `unavailable`, not to `false`. `(UI-4 r1/r3/r4)`
30. **Time-ordering is not correlation.** If the evidence is keyed on a different identifier
    than the thing displayed, there is no link: show it verbatim, label it "NOT correlated",
    leave the status unknown, and name the owner who must add the key. `(UI-4 r3)`
31. **A per-call outcome is not the end state.** "disconnect call SUCCEEDED" is not "the
    gateway is disconnected" — require the contract's own pinned boolean. `(UI-4 r4)`
32. **Log-message parsers must be whole-string strict** (`fullmatch`, closed vocabulary on
    every field the consumer reasons about). A `(.*)$` tail let drift suppress the loudest
    warning. `(UI-4 r5)`

## Making the panel good, not just correct

The operator has rejected a plan at three separate gates (UI-3, UI-4, UI-5) with the same
phrase: "make sure to utilize the /frontend-design skill to make a modern/beautiful app."

33. **Invoke `/frontend-design` DURING PLANNING, before ExitPlanMode** — and, before any
    chart, `dataviz`.
34. **Naming an aesthetic and one memorable element is not enough.** UI-5's plan did that and
    was still rejected, because the design section read as defensive ("cohesive with the
    existing system / additive CSS / refined execution") instead of a bold concrete vision.
    Lead with the ambition; put the constraints second. Specify: a committed COLOR story with
    atmosphere; dramatic TYPOGRAPHY treatment (scale, weight, tracking — character comes from
    treatment, since no font files are allowed); the SIGNATURE inline-SVG instrument in
    visual detail; orchestrated MOTION moments behind `prefers-reduced-motion`; and SPATIAL
    composition (asymmetry, an oversized hero, negative space against a dense control strip).
35. **The dashboard is strictly self-contained (SEC-002 / NFR-S3)** — no CDN, no remote fonts,
    no remote images. System/monospace stacks with tabular-nums, hand-authored inline SVG,
    CSS-only motion, dark and light theming. Grep the assets for `http`/`cdn`/`fonts.g` before
    committing.
36. **Screenshot every state before committing** — healthy, armed, unconfigured, degraded,
    plus the light theme — with a scratchpad Playwright script. UI-4's ring and hazard-rail
    geometry bugs were invisible in code and obvious in the render.
37. **Keep shared `styles.css` changes additive and scope new CSS to the new panel**, or
    sibling panels and their tests regress. After a rebase on the dashboard seam, check brace
    balance is 0 — an appended-CSS conflict can glue into an unterminated rule, and a lost `}`
    silently voids every later rule.

## Commands that mutate durable state

38. **Validate every value the command will PRINT before it mutates anything.** `resolve`
    deleted the demotion-pending lockout and then emitted its proof lines; a control character
    in the operator's acknowledgement made a line unprintable, so the command exited non-zero
    having already unblocked promotion. The operator reads a failure, the system reads "clear",
    and nothing records that a manual resolution happened. Same shape as rule 9's
    order-of-operations trap, on the output side. `(SRS-RESV-004 r4)`
39. **One alert body shared by branches that took different actions misdescribes at least one
    of them.** Both blocked demotion branches paged "the unfilled liquidation order is being
    canceled" — but the probe-inconsistency branch deliberately cancels nothing, so the page
    sent the operator after an order that is still live and unmentioned. A page is a RECOVERY
    instruction: derive it from the recorded outcome (including the FAILED case, where a live
    order most likely remains), never from the branch's intent. `(SRS-RESV-004 r3)`

40. **A control that DISPLAYS a confirmation warning must TRANSMIT the acknowledgement.**
    The UI-5 pane raised SYS-49e's "COOL-DOWN ACTIVE — manual swap during cool-down" on arm
    and then posted a body with no acknowledgement in it, so the moment the route started
    enforcing the window an operator who was shown the warning and confirmed it got a 428
    refusing them for not confirming. Bind the acknowledgement to the warning that was
    ACTUALLY shown, at ARM time, exactly as the target id is bound — a poll landing between
    arm and confirm must not silently change what the click means — and send it only for the
    KNOWN-active case: always would make every ordinary action a silent override, and an
    UNKNOWN state is not something anyone can knowingly override. `(RESV-006, found by the
    browser leg, invisible to every offline test)`

41. **A recovery instruction is EXECUTABLE TEXT — it inherits every bug the code has.**
    SRS-RESV-006 spent round 5 separating "the instant the swap started" from "the instant it
    completed", because a seven-day window stamped with the former is short by however long
    the swap took. Round 7 found the same defect surviving in the REMEDIATION: the failure
    message told the operator to reopen the window with
    `record-completion --completed-at <observed_at_seconds>`, so anyone who followed it
    reintroduced by hand what the fix had just removed. Carry the correct value on the failure
    outcome and print THAT — and when the value genuinely is not known, print no timestamp at
    all rather than the nearest one to hand. After fixing a value, grep the strings that TELL
    someone what to do with it. `(RESV-006 r7)`
41. **An error message that names a recovery is a promise; go and check it exists.** RESV-006
    added a fail-closed refusal telling the operator to "clear it if it did not complete" —
    and no subcommand could. The only write path would have recorded the failed swap as
    completed, so the honest options were hand-editing the durable file or lying to the tool.
    Whenever a refusal instructs, grep the surface for the thing it instructs, and if the
    path does not exist either build it in the same commit or word the message around the
    dead end. A fail-closed state with no reconciliation path is a wedge, not a guard.
    `(RESV-006 r22)`
42. **A reconciliation command must report what it FOUND, not just exit.** "Nothing matched
    what you named" and "cleared" must be distinguishable, or an operator walks away
    believing they resolved something they did not touch. Name the swap being reconciled and
    refuse a marker belonging to a different one — clearing whatever happens to be there on
    the strength of a request about something else is how the wrong safety window gets
    retired. `(RESV-006 r22)`
