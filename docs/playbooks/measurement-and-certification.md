# Measurement and certification substrates

Read this when the feature emits a verdict ABOUT the system: an availability certificate, a
latency NFR artifact, a reproducibility check, a coverage claim. These attract the heaviest
review in the repo — REL-001 took 20 rounds, PERF-001 5, BT-010 4 — because a certification
artifact is only honest if it CANNOT be made to say PASS without real, complete evidence.

## Availability / uptime certification (REL-001, ~18 findings over 20 rounds)

1. **No-data is not up.** Measure only over positively-observed coverage; unmeasured time is
   INCONCLUSIVE — a distinct verdict from FAIL — never counted as up. This is finding #1.
2. **No coverage-synthesis surface.** Delete any helper that turns the measured window into
   "covered", and any `--assume-full-coverage` flag. Coverage comes from observations only.
3. **Complete denominator, from the authority.** Derive the full unit set (all sessions,
   days, records) from the calendar; a caller-supplied list understates the denominator into
   a false PASS.
4. **EXACT measurement period, not `>=`.** A rolling-30-day metric certifies exactly one
   30-day window; a longer window dilutes a failing sub-period into a passing average.
5. **DST-robust period gate = UTC-midnight bounds + strict elapsed-ns.** Gating an
   Eastern-midnight window on elapsed nanoseconds makes 30 calendar days 29d23h at
   spring-forward and wrongly fails. Never accept caller-supplied `period_calendar_days` —
   it is forgeable.
6. **Provable FAIL beats missing evidence.** If observed downtime alone breaches the budget,
   FAIL — before the unmeasured-coverage INCONCLUSIVE check. A definite breach must never
   read as inconclusive.
7. **Excluded causes carve the denominator.** Planned-maintenance downtime inside the window
   must not be silently dropped from the numerator while inflating the ratio.
8. **The objective label locks the gates.** An artifact labelled `SRS-REL-001` validates the
   canonical gates in `__post_init__`; relaxed test configs use a DIFFERENT label so tests
   cannot mint an SRS-labelled PASS with weakened gates.
9. **Integer comparison at the boundary** — `(1000 - target) * effective >= 1000 * downtime`,
   exact at 0.999. Never an f64 compare.
10. **Fail-closed input parsing.** `payload.get(k, []) or []` coerces null / false / 0 / ""
    into "no evidence" → false PASS. Require the array when the key is present.
11. **Guards must survive `python -O`** — explicit `raise`, never bare `assert`.
12. **A corrupt evidence source is a refusal, not a crash.** Map store/OS/value errors to the
    CLI's refusal exit contract, with a corrupt-store regression.
13. **Do not reconstruct per-entity intervals from a source with no per-entity id.** SYSTEM
    log records carry `strategy_id=None`; drop them rather than mispair.
14. **FSM edges:** same-timestamp DOWN/UP is zero-duration (order DOWN before UP); an
    unclosed outage runs to window-end (never fabricate a recovery); repeated UPs are one
    boundary-open interval, not N.

## Latency / NFR verification (PERF-001)

15. **Budgets live in more than one spec document.** SRS-MD-001's 100 ms fan-out budget is
    PROSE in the SRS requirement row, not the SyRS table. Parse both, per-NFR.
16. **A budgetless metric is a smell** — grep the SRS row before modelling it as budget-free.
17. **Composite NFRs need EVERY leg**, bound to a threshold leg and assembled by a bundle
    that fails closed unless each declared leg is present exactly once.
18. **Percentile semantics are per-LEG, not per-NFR** — one NFR mixes a p95 leg with a flat
    `<= 5000` leg; an NFR-wide value lets a verifier evaluate the flat leg as p95.
19. **Simultaneity is a per-NFR property.** Enforce overlapping windows only where the spec
    says "simultaneously"; two distinct systems must NOT be forced simultaneous.
20. **Nearest-rank percentiles, and `checked_sub` at construction** — `end - start` can
    overflow i64 for extreme endpoints even when `end > start`.
21. **The check must PARSE the spec, not restate it.** Parse the doc table rows and assert
    the catalog matches, with negative spot-checks proving each guard catches drift.

## Reproducibility verifiers (BT-010)

22. **Never take a caller-supplied callback that can share mutable state with the work under
    test.** Compute the derived artifact after both runs and a run that mutates state the
    callback reads is MASKED (false PASS); compute it between runs and the callback
    CONTAMINATES the second run (false FAIL). No ordering fixes both — they are the same bug
    from two sides. Use a known-pure function over immutable inputs the harness controls.
23. **Scope in-process vs cross-process honestly from the start.** A same-process double run
    cannot catch nondeterminism that is stable within a process and varies across a restart,
    so it does not close a "platform-generated random values" clause. It also assumes the
    caller's factory and data source are deterministic — it does not prove identical inputs.
    Name the cross-process workflow and the input-provenance manifest as deferred owners.
24. **When you reverse a design decision, grep the contract description for the OLD framing**
    and scrub it. Round 4 blocked on a stale round-2 phrase sitting beside its round-3
    replacement.

## Across all three

25. **Tri-state, always: Satisfied / Violated / Unverified.** A verdict that returns "true"
    when it could not run the check is a false positive.
26. **The check's own output must not over-claim.** `<FEATURE> PASS` on a contract script
    that only proves the surface should read
    `<FEATURE> SDK-SURFACE PASS (contract evidence only; not a full requirement pass)` and
    print its deferred owners. The reviewer audits the verifier too.
27. **Honest CLI docs:** the `python/` tree is not pip-installed — document
    `PYTHONPATH=python python -m X` and add a subprocess test that runs independent of
    pytest's pythonpath.

- **Give a scan a completeness backstop, not a sixth pattern.** Five separate reviewer
  findings were "your regex did not anticipate this shape". The fix that ends the class is
  not a better regex: count the impls a LOOSE pattern can see, count what the strict one
  parsed, and `fail()` when the strict pass accounts for fewer. Any future shape the
  parser cannot read then turns the guard red instead of silently shrinking the set it
  claims is closed. `(SRS-MD-005 r20)`

- **A test cited as proof of an invariant must exercise the branch that could break it.**
  A module comment named `a_shortened_ttl_shortens_both_directions` as the thing pinning
  the runtime shorten-only invariant. That test configured 50 ms - below BOTH defaults -
  so the `min` on the negative branch never bit, and deleting it left the test green.
  Citing a test by name is a claim about what it covers; check the citation by mutating
  the thing it is supposed to pin. `(SRS-MD-005 r23)`
