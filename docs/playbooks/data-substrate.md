# Data substrate — ingestion, point-in-time reads, coverage, tiering

Also read [durable-writes.md](durable-writes.md); every store here sits on that recipe.

## Point-in-time reads (17 rounds on DATA-007 — pre-check all of these)

1. **Gate on availability, not on the event timestamp.** A fundamental's natural-key
   `event_ts` is the FISCAL PERIOD END, not the filing date. Use a separate `available_ts`
   (`available_ts <= as_of`), or a Dec-31 statement filed in February leaks into a January
   run. Validate `available_ts >= event_ts` — you cannot file before the period ends.
   `(DATA-007)`
2. **Bound the as-of window below, correctly.** A periodic input's in-force record can
   legitimately predate the price lookback: filter on `event_ts <= as_of` ONLY. `(DATA-007)`
3. **Split adjustment leaks the future.** `query_split_adjusted` adjusts through the
   coverage frontier `D >= end`, so a split effective in `(as_of, D]` re-bases the historical
   window. Add an as-of-capped read, and report `adjusted_through` (the basis) SEPARATELY
   from `coverage_through` (the proven frontier). `(DATA-007)`
4. **`as_of` must be DERIVED, not caller-forgeable.** A public scheduled run taking a free
   `as_of_ts` lets a caller pair `session=today` with a future as-of. Derive it from a
   calendar port; the caller passes only a relative lookback. `(DATA-007)`
5. **An unsafe-without-its-prerequisite core is `pub(crate)`, not `pub`** — with a domain
   test asserting the visibility. `(DATA-007)`
6. **Avoid O(universe × store) scans**: binary-search over the canonical
   `(kind, symbol, resolution)` block, with an inverted-range guard so the indexed slice
   cannot panic. And fail closed on an inverted range (`start > end`) rather than returning
   `Ok(None)`, which reads as "missing data". `(DATA-007)`
7. **Pre-count before materializing** in a bounded read — checking `max_bars` after the
   owned clone has already allocated defeats the bound. `(DATA-007)`

## Coverage as a trust boundary

8. **A normalized read without coverage is RAW-AS-ADJUSTED.** An absent split set is
   indistinguishable from "splits happened but were not ingested", so serving split-adjusted
   data mislabels raw bars. Until coverage exists, the math is crate-internal substrate
   exposed on no surface — CLI, binding, and the Rust crate API alike. Metadata must separate
   `public_request_modes` (what surfaces serve) from `core_library_modes` (what the math
   implements). `(DATA-012)`
9. **Bound applied corrections to the PROVEN frontier**, not to everything stored. A
   correction with `effective_ts > D` still adjusts in-window rows and pushes the result past
   the advertised `coverage_through`. `(DATA-011)`
10. **Point-of-use validation for a publicly-constructible trust record.** The frontier comes
    from a stored record whose type is `pub`, so enforce self-consistency inside the SHARED
    `validate_record` that runs at both `upsert` and `restore` — a constructor cannot help,
    callers bypass it. `(DATA-011)`
11. **ONE write surface for a trust assertion, refused at EVERY generic path.** The reviewer
    escalated across two rounds: the operator CLI, then the library ingestion API, then the
    fixture generator. The decisive boundary is the generic data-layer ingest refusing the
    kind, plus a `provider_ingestion_kinds()` that excludes it. `(DATA-011)`
12. **A publicly-constructible value type used in money math re-validates at the point of
    use**, and carries its own discriminator — a `SplitEvent` needs a `symbol` so AAPL's
    split can never touch an MSFT bar. `(DATA-012)`
13. **Money math needs GENERATED property coverage.** In a zero-dependency crate, hand-roll a
    seeded PRNG and assert identity-with-no-applicable-split, symbol isolation,
    compose-then-divide equivalence, order-independence, and exact round-half-to-even.
    `(DATA-012)`

## Ingestion and provider adapters

14. **Three-crate decomposition is forced by SRS-ARCH-002.** `atp-adapters` and `atp-data`
    each depend only on `atp-types`, so no crate depends on both: put a vendor-neutral DTO in
    `atp-types`, map provider → DTO in the adapter, build records in the data layer. The
    in-process handoff is the deferred orchestration host — honest scoping, not a gap. The
    integration test that proves availability lives in the one crate allowed to depend on
    both. `(DATA-005)`
15. **Per-field domain validation, not blanket sign rules.** Reject negative
    revenue / total_assets / total_liabilities; KEEP net_income, book_equity and cash flows
    signed — a loss is legitimate. Plus impossible provenance and positive denominators.
    `(DATA-005 r1)`
16. **Adapter methods return `AdapterResult`/`AdapterError`**, never the raw domain error.
    `(DATA-005 r1)`
17. **Restatements and multi-filing keys.** A natural key of
    `(kind, symbol, resolution, period_end_ts)` models ONE authoritative filing per period; a
    restatement is same-key/different-content → fail closed. The real fix is a filing-version
    dimension, i.e. a storage-schema change and its own slice. Pin the current fail-closed
    behaviour with a regression test and name the owner. `(DATA-005 r2)`
18. **Sharadar emits multiple rows per (ticker, period) by `dimension`.** Accept only `ARQ`;
    reject the rest fail-closed — `MR*` is most-recent-reported, back-filled, i.e. lookahead.
    `(DATA-005 r4)`

## Tiering and multi-store

19. **Split the error taxonomy.** UNREACHABLE (offline, recoverable) is not
    REACHABLE-BUT-BROKEN (corrupt, conflict, lock, alias — an integrity failure that fails
    closed). One classifier drives every path: write, sync, report, evict. `(DATA-008 r2)`
20. **A CLI that prints a failure status and returns `Ok(())` exits 0** — automation cannot
    see it. Return the error, and add a process-level exit test. `(DATA-008 r3)`
21. **State per-operation behaviour precisely.** "Fails closed on ALL non-ready" is false if
    one operation benignly no-ops. `(DATA-008 r4)`
22. **A tri-state verdict, never a boolean.** A verifier that returns `true` when it could
    not run the check is a false positive: Satisfied / Violated / **Unverified**.
    `(DATA-008 r6)`
23. **An "ALL X" AC is unmet while ANY path bypasses the new mechanism.** That is the close
    blocker — scope it and stay `passes:false`. `(DATA-008 r6)`
24. **A "X ⊇ Y" proof over two live stores cannot detect loss of data already evicted from
    Y.** Document the bound and defer the durable manifest. `(DATA-008 r8)`

## Routing existing CLIs through a new coordinator

25. **The blast radius is the killer, not the coordinator.** Sibling features assert on the
    exact text of a CLI's `cmd_ingest` — DATA-016 idempotency and DATA-017 writer
    serialization mutation-test the literal `&dir` lines at a specific indent, and ~12 test
    files drive those CLIs as store-population tools. Relocating the lock/load/save out of
    `cmd_ingest` breaks them. `(DATA-008 close)`
26. **The low-risk shape:** keep each existing `cmd_ingest` byte-for-byte, ADD a
    degrade-tolerant best-effort sync after the primary write (a strict erroring sync is
    wrong for a caller that already committed), keep the args and the parsed output keys, and
    guard the new invariant by scanning the SAME function body — a whole-file marker match is
    evadable by naming the marker in an unrelated command. `(DATA-008 close)`

## Corporate-action fact reads

27. **AS-HELD symbols, never retag.** The book holds the old name until the rename fact
    applies; retagging silently misses a pre-rename split. Parameterize the shared extractors
    rather than forking them. `(DATA-021 r1)`
28. **Structural events need the validity-window check too**, with a boundary-aware variant
    for a symbol change (`event_ts == valid_until` is allowed — the rename record retires its
    own segment). `(DATA-021 r2)`
29. **A retired predecessor bounds its own segment**, and renames must be walked FORWARD for
    a fact read so the successor's later in-window actions surface. `(DATA-021 r4/r5)`
30. **Same-instant precedence needs a secondary sort key**: SymbolChange → Split → Dividend
    → Merger → Delisting. `(DATA-021 r6)`
31. **A mixed-merger cash leg is ADDITIVE on the pre-conversion count**; pure cash is a full
    disposition → review, not an adjustment. `(DATA-021)`

## Derived series — accumulators and consolidators

An accumulator that PRODUCES the inputs to a trusted pure function has fabrication hazards
the pure function cannot see.

32. **Cross-stream ordering is the killer.** When two event streams feed one series (fills
    and marks), tracking their timestamps INDEPENDENTLY lets them interleave incoherently: a
    fill at t=10 then `mark(5)` records t=5 equity from a future position; `mark(10)` after a
    fill at t=10 records a mark that omits the fill. Downstream `compute` sees a monotonic
    curve and an in-window fill and cannot detect either. Serialize the streams: a fill must
    be strictly after every recorded mark, a mark at-or-after the latest applied fill (equal
    timestamps allowed — that is the legitimate apply-fills-then-mark order). `(BT-004 r1)`
33. **Read the producer's equity formula before writing yours.** Net-liq is
    `cash + Σ position.market_value`, matching the backtest engine exactly; put a
    `market_value_minor(mark)` primitive ON the position type so there is one marking
    discipline. `(BT-004)`
34. **A missing mark FAILS CLOSED, never values an open position at zero** — and validate
    every supplied mark as positive even for unheld symbols; a non-positive quote is corrupt
    regardless. `(BT-004)`
35. **Parity is the headline test.** Drive a real backtest AND the accumulator from the same
    activity and assert the metric families are equal, with and without costs. A standalone
    fixture run cannot catch parity drift. `(BT-004)`
36. **Consolidation: keep only buckets whose FULL period lies inside `[start, end]`.** A
    range-bounded consumer otherwise emits a partial edge bucket mislabelled at the period
    start, which looks like a complete higher-timeframe bar and corrupts warm-up.
    `(SDK-007 r1)`
37. **The closing bucket of a session has no next bar** — `update()` alone drops it. Document
    flush-at-session-close and pin it: update-only omits the final bar, update+flush equals
    the batch path. `(SDK-007 r2)`
38. **Use an independent oracle.** `df.resample(rule).agg(OHLC)` validates the pure engine
    (UTC index intraday, `tz_convert` for daily; empty buckets sum volume to 0 with NaN OHLC,
    so keep the default `dropna`). Indicators pin against pandas-ta / TA-Lib the same way.
    `(SDK-007)`
39. **Do not promise runtime delivery no runtime implements.** A `ctx.X` docstring promising
    a runtime-managed feed and flush is a block; scope it to its named owner and point
    authors at the paths that work today. `(SDK-007 r3)`
