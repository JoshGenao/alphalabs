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
