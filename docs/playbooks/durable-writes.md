# Durable writes and fail-closed reads

Reference implementation: `crates/atp-simulation/src/backtest_store.rs::save_to_path` /
`load_from_path`. Python equivalent: `python/atp_logging/persistence.py`.

## The write recipe (all four steps; `std::fs` only, zero deps)

1. Write to a **scratch** file → `file.sync_all()` (fsync the bytes) → `fs::rename` (atomic
   publish) → `File::open(dir)?.sync_all()` (fsync the directory, so the rename survives a
   crash). Skipping either fsync is a real `[high]`: a crash can publish unwritten bytes, or
   roll back the rename. `(BT-009)`
2. **Per-call-unique scratch name** — `<base>.<pid>.<seq>` via `process::id()` + a static
   `AtomicU64`. Not a clock or RNG read: the determinism check forbids `SystemTime` /
   `Instant::now` / `rand`, but `process::id`, atomics and `sync_all` are fine. `(BT-009)`
3. **Rotation must fsync the directory AFTER creating the new active segment**, not only
   after the renames. fsync on a file makes its contents durable, not its name — a crash
   can leave the first post-rotation record (a kill-switch activation) in a file whose
   directory entry never existed. `(LOG-001 r26)`
4. **Validate BEFORE you persist.** A permissive capture with validation only in the reader
   lets a bad value be published atomically over the last-good store; recovery then fails on
   self-written data. Return the error before touching the store, and test that the prior
   good store survives. `(SIM-004; DATA-018)`
5. **Stage → verify the staged bytes → publish.** Never write-then-check. Split
   `durable_write` into `stage`/`publish` so "verify between the halves" is expressible —
   and remember the restore path, which is easy to forget after fixing the export path.
   `(DATA-018)`
6. **`F_FULLFSYNC` is ENOTSUP on SMB / NFS / FUSE on macOS.** A network target needs a
   fallback, not a hard failure. `(DATA-018)`

## Reading

7. **A missing directory and a missing file are different facts.** No directory (unmounted,
   deleted) → fail closed; a missing file inside a present directory → genuinely empty
   store; present but corrupt → fail closed. Conflating them silently erases history.
   `(BT-009)`
8. **Absence must surface from the open, not from a pre-check.** `if exists(): open()` is a
   TOCTOU; a deletion or rotation in that window returns a successful-looking empty read.
   Thread a `require_active` flag into the reader and let the open raise. Keep `exists()`
   only as a nicer-message fast path. `(LOG-001 r20/r25)`
9. **Fail-open on read is the killer.** Re-validate every record reconstructed on read: a
   tampered or stale line that is valid JSON but violates the invariants must fail closed as
   corruption, not be served. Extract ONE `validate_record()` shared by dispatch, write, and
   read. `(LOG-001 session 1 r2/r3)`
10. **A torn tail splits at the last newline, on BYTES.** Do not `decode(strict)` the whole
    buffer — it raises and loses every good record. Decode only the complete portion; drop
    the unterminated tail; a complete-but-unparseable line is corruption. `(LOG-001 s1)`
11. **Enforce the store's own invariant on READ, before any caller filter.** A wrong-class
    record physically present in a store (legacy data, a bad recovery, a hand edit) was
    published unfiltered by one path and silently dropped by another — filtering first
    destroys the evidence that the separation this feature exists to keep was broken.
    `(LOG-001 r30/r34/r35)`
12. **Path-alias separation bypass.** `system.jsonl` vs `./system.jsonl` is the same file.
    Guard with bare-basename validation (reject separators, absolute, `.`/`..`) AND
    `os.path.samefile()` after both are open — which also catches symlinks and
    case-insensitive collisions. In Rust: lexical `components().eq()` **plus**
    `canonicalize`, re-checked at the dangerous operation, because a post-construction
    symlink escapes a constructor check. `(LOG-001 s1 r1; DATA-008 r1)`
13. **Untrusted counts must never drive `Vec::with_capacity`.** A checksum-valid count of
    `usize::MAX` is an OOM abort instead of a fail-closed read. Grow incrementally; a short
    blob then exhausts the cursor and fails closed. Depth-bound any recursive parser.
    `(BT-009; SIM-004)`
14. **Re-derive what you can re-derive.** If a stored aggregate (running cash) is derivable
    from the stored records, a checksum-valid but inconsistent value restores fabricated
    state. Re-derive and reject on mismatch. `(SIM-004)`
15. **Keep a migration branch when you bump the schema version.** Rejecting v1 strands
    existing snapshots, which defeats recovery. Regression-test a literal v1 blob.
    `(SIM-004)`

## Bounded reads

16. **A cap applied AFTER a full-store read is not a cap.** Stream and bound: a generator
    over segments plus a `deque(maxlen=limit)` gives O(limit) memory while keeping the total
    exact (counting is free). `(LOG-001 r4)`
17. **A polled surface must read the PAGE, not the trail.** O(trail) I/O may be an honest
    limit for an explicit operator query; on a 4-second poll against an append-only store it
    is unbounded growing load. Add a real tail reader (segments newest-first, backwards in
    fixed blocks, stop when the page is full) and verify it against the forward reader for
    exact record AND position equivalence. `(LOG-001 r24)`
18. **Measure bytes read, not rows returned.** A row-count assertion passes on a full scan.
    `(LOG-001 r24/r36)`
19. **State the trade you kept.** A tail read reports no exact total and validates only its
    window — publish that, don't let a green pane imply a verified trail. `(LOG-001 r32)`

## Backup and archive surfaces

20. **Absence of evidence is never evidence.** Zero discovered units → `Unverified`, never a
    vacuous pass. An unreadable subtree fails the whole run — backing up "the readable part"
    and reporting Verified advances the RPO clock over data never exported. `(DATA-018)`
21. **Judge freshness against the units currently on the SOURCE**, not against the ledger; a
    ledger cannot speak about a unit it has never seen. Verify the UNION of expected and
    present archive units. Re-read the media in `status` — a ledger entry is not evidence
    the archive still exists. `(DATA-018)`
22. **Failure-domain guards are multi-level:** reject target == source, aliases, and target
    nested in source; canonicalize the deepest EXISTING ancestor (a first-run target
    legitimately does not exist yet); guard the per-unit subdirectory; and use
    `symlink_metadata` on every archive read path — `is_file()` and `read_to_string` follow
    symlinks, so an entry linking back into the source checksums perfectly while holding no
    bytes of its own. Never `create_dir_all` the target root: an absent mount is `Degraded`,
    not something to materialise on local disk. `(DATA-018)`
23. **Compare RAW SECONDS against the objective** — flooring to whole days holds a green
    status ~24h past a breach. A time-driven CLI must read the real clock; a frozen `--now`
    default makes a cron entry see zero elapsed time forever. `(DATA-018)`
24. **Unit identity includes the FILENAME**, not just the directory, or a verified blob
    vouches for a failed sibling. Export + verify + ledger-advance must be ONE locked unit.
    `(DATA-018)`
25. **A lower crate cannot decode a higher crate's blob.** Do not report `Verified` on
    envelope-only evidence: fail closed to `Unverified`, add a validator injection seam,
    move the operator CLI up to the composition root that can supply the real decoder, and
    carry a `VerificationDepth` so weaker evidence never renders identically to a full
    decode. `(DATA-018)`

## Markers that must survive a crash (RESV-006 r6/r13)

26. **A guarantee that spans two writes needs two writes.** If a safety marker must exist
    whenever a durable action happened, and the marker and the action are separate writes,
    then NO single ordering is correct — ordering only chooses which direction fails.
    Marker-then-action leaves a marker for an action that never happened; action-then-marker
    leaves an action with no marker, which is the fail-OPEN one. Write it **twice**: a
    provisional marker before, confirmed after, and abandoned explicitly when the action does
    not complete. RESV-006 spent r6 fixing one direction and r13 discovering it had opened
    the other; RESV-004's engage-then-amend lockout is the same shape. `(RESV-006 r13)`
27. **Once phase one exists, "drop it" stops meaning "nothing happened".** Every path that
    previously relied on dropping a token to write nothing now silently leaves the
    provisional marker standing — including the ones the fix never looked at. Re-walk each
    of them and make the abandon EXPLICIT. Do not make it a `Drop` impl: an implicit abandon
    also fires on panics and early returns, where the durable state is *unknown* rather than
    known-unchanged, and clearing a marker you cannot reason about is the fail-open again.
    `(RESV-006 r13)`
28. **A provisional marker is a new durable STATE, so give it a surface.** It suppresses
    exactly like a confirmed one — over-suppressing after a maybe-action is recoverable,
    under-suppressing after a real one is not — but only an operator can find out what
    actually happened, and they cannot resolve what they cannot distinguish. Publish the flag
    on every reader, tri-state: `true`, `false`, and **unknown** for a store that could not
    be read. Never collapse unknown to `false`. `(RESV-006 r13)`
29. **Two facts with different LIFETIMES cannot share one durable slot.** A provisional
    marker is discarded when its action fails; a confirmed record is never discarded. Store
    them in one field with a flag saying which you mean, and clearing the first clears the
    second. RESV-006 shipped exactly that: the provisional write reused `last_completion`,
    so an acknowledged manual swap inside a running cool-down overwrote the window it was
    running inside, and abandoning that attempt when it failed deleted a live seven-day
    window — the automatic triggers resuming days early. Separate slots; the abandon touches
    only the provisional one; the resolver takes whichever runs LATER, so the pair can never
    resolve to less protection than either alone. `(RESV-006 r15)`
30. **After a two-phase conversion, re-ask the original safety question on every path.**
    The conversion changes what "the record" means, so every read and every clear that was
    correct against one slot must be re-derived. The concurrent-operation path is the one
    that bites: RESV-006's own requirement guarantees a manual swap stays available DURING
    a window, which is precisely the case that made two records coexist — and the case
    nobody had a test for. If your feature permits an operation during the state it
    protects, write that test first. `(RESV-006 r15)`
31. **"Is this record mine?" needs provenance, not identity.** Matching a durable marker by
    the entity it names is not the same as matching the marker you wrote. Under any
    retry/monotonicity rule the two diverge: the attempt that lost the race wrote nothing,
    yet identity-matching lets its cleanup delete the winner's marker. Carry what you wrote
    on the token and clear on FULL equality, so an attempt that wrote nothing clears nothing
    — that beats threading the write's outcome through every path that might clean up.
    `(RESV-006 r18)`
32. **A monotonicity rule is about the aggregate, not about one field.** Once a record has
    two slots that can each be "the current one", every guard that says "do not go backwards"
    must compare against whichever slot actually governs — and the reader already knows which
    that is. RESV-006 shipped a reader taking the later of two slots while its writers guarded
    only one, so an older write cleared a newer marker on its way past and shortened a live
    safety window. Extract ONE selector and route the reader and every writer through it; a
    comment saying "keep these in sync" is not a mechanism. Reviews r18 and r19 were the same
    defect on the failure arm and the success arm, found a round apart. `(RESV-006 r19)`
