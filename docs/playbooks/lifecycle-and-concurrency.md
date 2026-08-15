# Lifecycle and concurrency — publishers, cursors, claims, deadlines

Most of this comes from SRS-LOG-001's publisher, which drew 15 of its 38 rounds on its own
lifecycle. If you are adding a background ticker, a resume cursor, or a readiness claim,
read all of it: these bugs all present as a perfectly healthy-looking loop.

## Resume cursors

1. **Value is not identity.** Two byte-identical records — a retried operation writing the
   same message with the same correlation id in the same nanosecond — are two events. A
   value-compared anchor treats the second as already-published and drops it. Key on
   PHYSICAL position (segment device + inode + end byte offset). `(LOG-001 r5)`
2. **Position alone is not identity either.** Rotation unlinks a segment, the filesystem
   reuses the inode, and a new record lands at the same offset. Compare position **and**
   content; a position collision carrying different content means the anchor was evicted.
   The residue (inode reuse AND offset collision AND identical content) needs a monotonic
   sequence in the record envelope — a format change, therefore a different feature.
   `(LOG-001 r18)`
3. **A COUNT is not a cursor.** Rotation drops the oldest segments while new records append,
   so the list can stay the same length while its contents shift left underneath the index.
   `(LOG-001 r3)`
4. **Handle all three anchor outcomes.** Anchor at its index → publish after it. Anchor
   found elsewhere → rotation shifted the trail, publish after its new position. Anchor GONE
   → it was evicted; rotation drops a PREFIX, so everything still retained was written after
   it and has never been published — publish all of it and report the unrecoverable window
   as a gap. Re-syncing to the end would skip exactly the records the operator can still
   see. `(LOG-001 r3)`
5. **Enumerating paths then opening them is a race.** A rotation landing in that window
   makes the scan incoherent. Take the active segment's (device, inode) before and after
   each scan; if it moved, DISCARD the read (publish nothing, cursor untouched) and retry.
   Discarding is safe on an append-only trail — a later scan sees a superset. `(LOG-001 r9)`
6. **Do not resume by re-scanning from the head.** An anchor that tells you where you
   stopped, found by reading the whole trail every tick, is the same unbounded cost in
   disguise — until the loop's real cadence drifts past its interval while `health()` still
   says ok. Seek to the physical slot in O(line). `(LOG-001 r36)`
7. **Seed the cursor at the tail on start** for an event-driven channel, or the first poll
   fans out the entire archive while a kill-switch activation written a second after startup
   queues behind it — with every health signal green. Declare the skipped history; leave an
   unreadable store unseeded so the failure reports through the normal path; and leave a
   store that does not exist yet unseeded, since everything it later receives is genuinely
   post-startup. `(LOG-001 r35/r37)`
8. **Seeding is a precondition, not best effort.** A swallowed seed error leaves the cursor
   unset, and unset means "publish everything retained" — so a transient failure degrades
   into an archive replay with no counter raised. `(LOG-001 r37)`

## Readiness claims

9. **Claim from delivery, not from wiring.** `start()` claiming the channel means the
   runtime reports it served over a trail nobody can read. Claim on the first poll that is
   clean in EVERY dimension — read, publish, rotation, eviction — and run that first poll
   synchronously so the claim is deterministic rather than racing the thread.
   `(LOG-001 r10/r14)`
10. **A claim must be revocable.** Readiness that cannot be revoked eventually lies: once
    claimed, the channel stayed claimed after the publisher lost its store. `(LOG-001 r15)`
11. **Decide WHICH faults move readiness, and write the reason down.** Read failures and
    publish failures do — the channel is not delivering *now*. A rotation race also does: it
    means the poll published nothing, and it is transient so it cannot latch. An eviction
    gap does NOT: it is a fact about lost *history* and persists, so gating on it would pin
    readiness off forever — the same lie in reverse. `(LOG-001 r15/r23)`
12. **Release on EVERY non-delivery path**, including `stop()` and the catch-all that keeps
    the ticker alive. Surviving is not delivering: a loop erroring on every poll kept
    reporting the channel served. Release before joining, and regardless of how the join
    goes — a timed-out join then errs toward UNDERstating readiness, which is the safe
    direction. `(LOG-001 r17/r28)`
13. **Serialize the decision and the callback under ONE lock.** Setting the flag under a
    lock and then calling the registry unlocked lets a `stop()` land in the window and be
    undone by the pending callback. Note the consequence: `stop()` then blocks while a poll
    is mid-claim, so a claim callback must never wait on stop. `(LOG-001 r22)`
14. **Ordering inside start/stop matters.** `start()` must clear the stop flag BEFORE its
    first poll or a restart can never re-claim; the claim path must refuse while a stop is
    pending. `(LOG-001 r17)`

## Threads

15. **A guarded drain, not a guarded thread.** An unguarded publish call kills the daemon
    thread *after* `start()` claimed the channel — the runtime keeps reporting fully served
    while nothing publishes. Guard the publish, record the failure, do NOT advance the
    cursor past the record that failed (so it retries, never skips), and break that drain
    instead of the thread. Wrap the poll body as a last line of defence. `(LOG-001 r2)`
16. **A timed-out join must KEEP the thread reference.** Dropping it lets the next `start()`
    launch a second ticker beside the wedged first — two loops sharing one cursor,
    duplicating and skipping audit events. Count the timeout, refuse to start while the old
    thread is alive, and reclaim the slot only once it has genuinely exited. `(LOG-001 r14)`
17. **Make the public poll method self-serializing.** Only the ticker calls it in production,
    but tests and any future flush-now path call it directly — two concurrent reads before
    either advances the cursor publish the same record twice. `(LOG-001 r35)`
18. **Startup must be all-or-nothing.** Starting publishers before binding leaves tickers
    polling behind a process that never came up. Stop whatever started, then re-raise.
    `(LOG-001 r31)`
19. **A revocable registry is mutated cross-thread.** Individual set operations are atomic
    under the GIL, so nothing corrupts — but a status report that inspects the set once per
    channel can straddle a transition and describe a moment that never happened. Guard
    register/unregister/query, and take ONE snapshot for the whole report. `(LOG-001 r27)`
20. **`health()` must report liveness directly**, not let it be inferred from an absence of
    errors, and its `ok` should be sticky-false once data may have been missed.
    `(LOG-001 r2)`

## Deadlines

21. **A socket timeout is NOT a deadline.** It applies per operation, so an 8-round-trip
    SMTP conversation licenses 8× the budget. Create the budget once per send and arm each
    operation with what is LEFT. Never hand `Duration::ZERO` to `set_read_timeout` — on
    every supported platform that means block forever, inverting the contract exactly when
    the budget runs out. `(NOTIF-001)`
22. **`read_line` / `read_to_end` are unusable on a deadline path.** They issue many `read`
    syscalls and the countdown restarts on each, so a peer dribbling one byte per interval
    holds the connection open indefinitely while every operation looks healthy. Drive
    `fill_buf` + `consume` with the elapsed budget re-checked between reads. `(NOTIF-001)`
23. **Enforce the deadline BEFORE accepting a success observation** — sleep overshoot plus
    unit truncation can smuggle a late success past a probe loop. `(SAFE-002)`
24. **A bounded-wait on a MUTATING operation that expires is UNKNOWN, never
    did-not-happen.** Verified live: a `cancel_order` reported `ConnectivityBlocked` after
    its 15s deadline while the broker had actually cancelled it. Recovery must RE-READ
    broker state before acting, never blind-retry. `(EXE-007, 2026-07-30)`

## Poll-driven monitors (a fresh CLI process per tick)

MD-003's dashboard watchdog took 9 rounds, all on this shape. Any future poll bridge
(readiness probes, a liveness feed, a delivery watchdog) hits the same class.

25. **An empty or partial watch set is `Unavailable`, never FRESH with
    `watched_feeds: 0`.** A script missing a REQUIRED feed is the same. `(MD-003 r1)`
26. **Cross-poll transition dedupe lives in the PROVIDER** — the CLI's in-process baseline
    resets every poll. Advance the baseline on the FACT of a flip, and send failed log
    writes to a bounded **pending-record retry queue** with a surfaced dropped count. Holding
    the baseline back instead loses BOTH records of a transient incident that recovers before
    the retry. `(MD-003 r2)`
27. **`log_write_ok` is a per-poll aggregate (queue-empty)** — never reset by a later feed's
    success. `(MD-003 r3)`
28. **Monotonic-evaluation guard.** With a WS ticker, a REST poll and a health check all
    polling, only a strictly-newer `evaluated_at_ns` may commit baselines or logs **or be
    served to any surface** — otherwise an older fresh snapshot regresses a newer stale
    display. Write the regressions with an ADVANCING fake clock; a fixed-clock fixture masks
    this guard entirely. `(MD-003 r4)`
29. **The subprocess timeout must sit BELOW the channel cadence** — contract-test it.
    `(MD-003 r5)`
30. **Log on the SPECIFIC signal, display on the MERGED verdict.** A gap-only incident is the
    gap detector's record, not the staleness record. `(MD-003 r6)`
31. **A mandatory-AC log sink is REQUIRED** — no `None` default; boot composition fails
    closed without it. `(MD-003 r7)`
32. **Shared-socket operations eat each other's frames** — two readers on one session
    consume each other's responses. `(MD-003 live)`

## Guards that read before they write (RESV-006 r20)

- **A read-modify-write split across two lock acquisitions is not locked.** Reading a value,
  then calling a helper that takes the lock and writes it, leaves the whole gap open: a
  concurrent change lands there and is then overwritten with what you read. The function
  that READS is the function that must hold the guard. RESV-006's writability pre-flight
  loaded the cool-down period unlocked and handed it to `set_period`, silently reverting an
  operator's `configure --set-days`. `(RESV-006 r20)`
- **A probe must not be able to change what it probes.** Proving a store is writable means
  writing back exactly the record you read — not a reconstructed one, and not a default. A
  probe that writes a fresh record passes every "is it writable" assertion while erasing the
  window it was checking. `(RESV-006 r20)`
- **Two ways a static guard silently exempts the very thing it is for:** a parser that only
  sees `pub fn` skips private helpers, and "delegation credit" (this function is fine because
  the one it calls holds the lock) legitimises the exact split above. Both were true of
  RESV-006's writer check, and the unguarded writer was private AND delegating.
  `(RESV-006 r20)`
