# Contract drift — declarations, frozen snapshots, and stale prose

The moment a placeholder gets a real handler, its schema becomes a false statement. This is
the single most-repeated class in the repo's review history: LOG-001 hit it at r2, r6, r8,
r11, r15, r19, r22, r29, r33; EXE-003 at r1, r3, r5; RESV-003 at r7, r10, r13.

## Declared vs emitted

1. **Compare the full field SET, in BOTH directions, per route.** A superset is undeclared
   drift; a subset is an unkept promise. Serve a REAL request in the test and diff the
   emitted body against the declaration. `(LOG-001 r6; RESV-003 r7/r10/r13)`
2. **Compare each LEVEL separately.** `Route.response_fields` is flat, so declaring
   per-event fields there documents them *beside* the `events` array instead of inside it —
   a generated client looks in the wrong place. Declare nested item fields as item fields
   and assert top-level set equality plus item-level set equality. `(LOG-001 r8)`
3. **A type test that iterates the emitted body cannot see a documented-but-absent field.**
   Iterate the schema too. `(LOG-001 r8, on its own r7 fix)`
4. **Placeholder types contradict a live handler.** A route with no handler documents every
   field as `string`; once it answers with arrays, ints, and booleans, populate the
   `field_types` seam and regenerate. A placeholder that contradicts a live handler is worse
   than no schema. `(LOG-001 r7)`
5. **Nullable unions must be declared** — a field that is `null` on the commonest event is
   schema-invalid as a bare `string`. Both the OpenAPI and AsyncAPI generators need the
   union rendering. `(LOG-001 r15)`
6. **The WS envelope is `{type, channel, data}`** — couple a contract test to the snapshot's
   `required`, not to your memory of it. `(API-001)`
7. **If the payload carries a discriminator the client routes on, declare it.** `log_class`
   was load-bearing for the SPA and absent from the channel contract. `(LOG-001 r2)`
8. **Which way to fix it:** when the extra field is load-bearing honesty (metadata,
   counters, scope flags), move the CONTRACT to meet the code and regenerate. When it is a
   capability nobody can use, remove the SURFACE. `(LOG-001 r6)`

## When a deferred surface goes live

9. **Sweep every place that calls it deferred, in one pass, then WRITE THE CHECK.** Fixing
   instances one at a time does not converge — LOG-001 found the same "--follow is
   deferred" claim at r11 (contract flags), r19 (a sibling contract block), r29 (a block
   description), and r33 (three frozen public documents). The r29 collector, written once,
   immediately caught a third instance the manual sweeps had missed. `(LOG-001 r11/r19/r29)`
10. **The places to sweep**, exhaustively: `architecture/runtime_services.json` — every
    block's `deferred[]` **and** its `description` prose and per-rule `note` fields; the
    module docstrings (often two passages: a status section and a deferred list); the check
    tool's docstring **and its printed evidence string**; test docstrings; the package
    README; and the frozen public artefacts (`openapi.json`, the AsyncAPI snapshot, the CLI
    `manual.json`). Grep `deferred|contract only|will be|not yet` until clean.
    `(EXE-003 r1/r3/r5; BT-001; LOG-001 r33)`
11. **Prefer a declarative seam over a prose edit.** `Route.served_by` / `Command.served_by`
    / `EventChannel.served_by` swap the "Contract only" placeholder for a sentence naming
    the implementer — while still stating that a deployment which has not composed the
    handler returns 501, so "served" is never read as "served unconditionally". Guard the
    declaration AND the generated artefact, so a regeneration cannot silently restore the
    placeholder. `(LOG-001 r33)`
12. **Drift runs in both directions.** After removing a capability, the contract still
    promised a structured rejection for it — a consumer would look for a machine-readable
    error and get a usage failure. Cross-check all the places at once. `(LOG-001 r11)`
13. **Scope what you will NOT sweep, out loud.** The same staleness affected three other
    features' frozen evidence; re-freezing their snapshots inside this feature's diff is
    wrong. Say so in the contract and name the owners. `(LOG-001 r33)`

## Owner maps and the check tools

14. **Every named owner must appear in `deferred[]`** — including a `passes:true` feature
    that owns a still-deferred operation. Drop any "closed upstream" exemption.
    `(API-001)`
15. **When two contract blocks disagree, make one of them authoritative and pin it.** The
    LOGS owner flip-flopped across three rounds because two blocks and a check's print
    string each said something different. `(API-001)`
16. **Derive allow-lists from the enum, not from a copy of the list.** A `--severity`
    summary that omits CRITICAL — where kill-switch activations land — is a wrong manual on
    the one surface an operator uses to find them. Check the summary against the `Severity`
    enum in both the declaration and the frozen artefact. `(LOG-001 r36)`
17. **A new check script must be wired into BOTH `.github/workflows/ci.yml` and
    `tools/run_ci_locally.sh`, in the same slot.** The deterministic critic has no parity
    rule; the judgment reviewer blocks on it round 1. Data-layer checks aggregate through
    `tools/architecture_check.py` instead — follow the neighbours. `(ERR-9, SESSION 31)`
18. **Regenerate frozen snapshots with their own `--update` tool** and check the diff is
    exactly what you intended (one line, not a reflow). `(LOG-001 r33)`
19. **Never hand a file list that can contain `.json` to `ruff format`.** JSON is valid
    Python syntax; ruff rewrote `runtime_services.json` and the AsyncAPI snapshot into
    invalid JSON, ~10k lines of churn. Explicit `.py` paths only. `(LOG-001, self-inflicted)`

## Versioned formats

20. **Adding a variant to a durable format needs a schema-version boundary.** An old reader
    must hit a clean version-gate rejection, not "corrupt". Write the MINIMUM version the
    contents require, so a store without the new kind stays readable by old tools; accept a
    range on restore; reject an old-version blob carrying the new-version-only kind.
    `(DATA-012 §3)`
21. **Three states, never two:** `Absent` (no version field) / `Valid(n)` / `Invalid`
    (present but unparseable). Collapsing `Invalid` into `Absent` makes every parse failure
    a false "readable", and a gate that says readable is worse than no gate. `(DATA-015)`
22. **Locate the version by declared LAYOUT, never by plausibility** — a forward scan for
    "the first integer that looks like a version" reads a record count. `(DATA-015)`
23. **Scan every record, not the first**, and refuse ambiguity: duplicate keys (Python's
    `json.loads` is last-value-wins — use `object_pairs_hook`), trailing bytes after `}`,
    a key found inside a string value. `(DATA-015)`
24. **The schema registry (SRS-DATA-015) gates every new persisted format repo-wide** — a
    new on-disk shape is its concern, not yours to invent locally.

## Flipping a mode, a gate, or a widely-referenced feature

25. **Sweep the whole cluster in ONE pass; piecemeal does not converge.** Serving one new
    normalization mode touched FOUR contract blocks, five check tools (each docstring,
    argparse, PASS/FAIL label and `_DEFERRED_OWNERS`), and two Rust module docs — and the
    reviewer found exactly one stale reference per round while the others still said
    "deferred". Finish with a script that classifies EVERY reference to the token as
    served-or-deferred. `(DATA-012 mode flip, 6 rounds)`
26. **Keep contract tokens OUT of comments.** A check greps a compacted source token; put
    that same string in a new comment and the mutation test that replaces the code form
    leaves the comment behind, so the guard never fires and the mutation test fails.
    `(DATA-012)`
27. **Changing a port signature breaks sibling mutation anchors.** When a gate body changes
    (`validate(&record)` → `validate(record)`), `grep -rn` the old literal across `tests/`
    and `tools/` and update every anchor — a stale anchor makes the mutation a vacuous
    no-op, which is worse than a failure. The structural checker often still passes (it
    scans for token presence), so run it rather than assuming it broke. Every stub impl of
    the port needs its one-line signature update. `(DATA-013)`
28. **An AC clause served at the CLI but not at the binding is PARTIALLY served** — a
    `passes:false` contributor. Do not word it "met" or "closed". `(DATA-012)`
29. **`runtime_services.json` is ~512 KB and does NOT round-trip through `json.dumps`**
    (~6 KB of drift). Splice a new block TEXTUALLY before the root `}`, then validate with
    `json.loads` and confirm 0 deletions. `(PERF-001)` The symptom is unmistakable
    once you look: short arrays that were inline explode one-element-per-line, and
    `git diff --numstat` reads ~1200/380 for a change you know added six keys. **Check
    `--numstat` after every registry edit** — a semantic diff (`json.load` both sides and
    walk) tells you what actually changed, and the Edit tool applied to the original text
    is how you land it. Re-formatting the whole file also buries the real change from the
    reviewer, which is the part that costs a round. `(PERF-001; re-learned RESV-006 r13)`
30. **A `deferred` entry is a CLAIM, and closing the gap makes it a lie** — in the
    direction that costs most, because the registry is what the next reviewer reads as the
    statement of what is deliberately still open. Update it in the same commit that closes
    the residual, and say what actually remains now rather than deleting the entry: "the
    race is closed, what is left is bounded and in the safe direction" is the useful
    sentence. Grep the claim's WORDING across the tree — RESV-006 carried the same stale
    sentence in the registry, a module docstring, a test comment and the session note.
    `(RESV-006 r14)`
31. **Prose in a machine-readable registry is still checkable — check the identifiers.**
    Every `Type::method` a contract block names, in values and deferred prose alike, must
    still exist in the source it points at. Two traps: check BOTH halves (RESV-006's stale
    entry named a renamed TYPE whose method still existed privately, so a method-only scan
    passed), and enumerate foreign types explicitly instead of skipping anything the crate
    does not declare — a renamed local type is also undeclared, which is the whole defect.
    Beware the guard firing on its own explanatory note: write the example without the
    `Type::method` form. `(RESV-006 r14)`

## A contract that names a FILE breaks when a module is extracted (SRS-MD-005)

- **Declare the modules, not the path.** `subscription_fanout_check.py` read
  `crates/<crate>/src/lib.rs` directly, so moving the registry into its own
  module turned a sibling feature's closed-green contract red for a change that
  did not touch it. The contract block now carries a `registry_modules` list and
  the check reads what the contract names. If a check hard-codes a path, the
  next refactor is a false red — and the session that hits it has to decide
  whether it broke something. `(SRS-MD-005 r6)`
- **Extracting a module is a contract change even when no behaviour moves.**
  Grep the check tools for the crate path before you move a type, not after.
  `(SRS-MD-005 r6)`


- **Enumerate the recorders; do not fix the one that was caught.** `evidence.py` has four
  commands that write the record, and three re-rendered `EVIDENCE.md` afterwards.
  `cmd_critic` - the LAST one to run before a close - did not, so the page a reviewer
  opens in the PR read `critics: none recorded` while `evidence.json` beside it, in the
  same commit, held a `block`. The guard is an AST walk over `cmd_*` asserting that
  anything calling `save_record` also refreshes the page; it immediately found a second
  instance the sweep had missed. When a rendered artifact can lag its source, the class is
  "every writer of the source", never "this writer". `(SRS-MD-005 r14)`
- **A row that promises a command must be checked against the state that command reads.**
  The verification queue told the operator "Nothing" was missing and handed over
  `close_feature.py --verified --attested-by operator`, which exits 3 while a critic
  verdict is `block` - `--attested-by` relaxes which STEPS count, never the critic gate.
  The guard now cross-reads every `close_feature.py <ID>` in the queue against
  `.harness/runs/<ID>/evidence.json`. Prose that instructs is prose that can be wrong in
  a way the reader only discovers by running it. `(SRS-MD-005 r14)`

- **A rustdoc that argues for a design outlives the design by rounds.** The reachability
  cache changed policy in r14; the constant's rustdoc still called the OLD policy "the
  whole design" in r16, `state()` still promised "a fresh probe on every read", and
  `last_outcome()` still told callers `None` meant the gateway had answered again. Three
  separate reviews, three separate blocks, one root cause: prose that ARGUES is prose a
  reader trusts, so it is worse than absent when it is stale. When you change a policy,
  grep the constant's name and every method that reads it, and re-read those doc comments
  in the same edit. `(SRS-MD-005 r15, r16)`
- **A session note's "Key decisions" is a claim about the shipped code, not a diary.**
  The note still recorded "Only an UNREACHABLE observation is reused" two rounds after
  the code stopped doing that, and the same file said the opposite 200 lines later. Record
  the decision that SHIPPED, and where it was reversed, say so in the same bullet.
  Per-round narration belongs in the round log below it. `(SRS-MD-005 r16)`
- **Distinguish a TOTAL from an ordinal before writing a consistency guard.** "Adversarial
  rounds: 13" is a claim that goes stale; "round 12 found X" is narration that stays true
  forever. A guard matching both raised 38 accusations against session notes doing nothing
  wrong - and one that matched only `<digit> rounds` missed two of the three claims that
  had actually drifted. Match `rounds: N` and `at|after N rounds`; leave ordinals alone.
  `(SRS-MD-005 r16)`

- **A verification transcript must certify the tree it SHIPS with, and re-running is the
  only honest repair.** `VERIFICATION.md` said its commands "were re-run against the
  integrated tree (`ed36c790`)" while the diff carrying it had rewritten 215 lines of the
  module those captures covered; its pytest block reported 101 collected where the same
  command now collected 110. The captures were real - they just certified different code,
  which a reader cannot see. Re-run every block (scripted, so nothing is retyped), and
  re-apply each deliberate mutation around its own command for the sections that capture
  a broken tree. `tests/unit/test_evidence_artifacts.py::test_a_verification_transcript_certifies_the_tree_it_ships_with`
  now compares the transcript's own `git rev-parse HEAD` capture against
  `code_changed_since`. `(SRS-MD-005 r16)`

- **A rendered page that EMBEDS a checker's output cannot also be checked by it.** Adding
  "is EVIDENCE.md current?" to `verify` made `render_markdown` -> `verify` -> render
  recurse without bound, and once that was guarded, the page began reporting its own
  staleness - which changed what a fresh render said, so no fixed point existed and the
  page could never be current. The flag has to wrap the RENDER, not the check: while a
  render is in progress the self-check stands down, so the page never makes a claim about
  itself. `(SRS-MD-005 r19)`
- **A command that rebuilds a record entry silently drops the fields you did not pass.**
  `evidence.py critic` builds a fresh entry, so the re-stamp prescribed by the
  verification queue erased `rounds` - and the corroboration check only compares counts
  that are PRESENT, so following the documented command turned off the guard the same
  change had just added. Carry unspecified fields forward, and test that an explicit value
  still wins. `(SRS-MD-005 r19)`

- **Delete the hand-maintained number, keep the command.** A session-note section stated a
  playbook-entry count three times and was stale all three times - each version printing,
  two lines above the wrong figure, the exact command that would have corrected it. A
  number a human maintains beside a command that computes it will disagree with the
  command; the only fix that holds is to stop stating it. Name WHAT changed, which does
  not rot, and let the reader run the count. `(SRS-MD-005 r22)`
- **An invariant asserted in five places and held in none.** "`ttl_for` takes a `min`, so
  both directions only ever shorten" appeared in two module comments, a constant's
  rustdoc, a unit test and an L7 docstring - while the function applied `min` to one
  branch only, so `with_probe_ttl(2s)` LENGTHENED the negative window past its own
  default. Nothing unsafe shipped (a stale `Unreachable` errs toward blocking) but the
  claim was false everywhere it appeared. When a stated invariant turns out not to hold,
  ask first whether the CODE should be changed to match it - here that was a one-word fix,
  against softening the same sentence in five places. `(SRS-MD-005 r22)`
