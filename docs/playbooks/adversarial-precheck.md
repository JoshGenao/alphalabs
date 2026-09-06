# Adversarial pre-check — always, before the first review round

The judgment critic finds real defects one per round. Rounds are expensive (a full review
of the diff, a fix, a regression test, a re-run). The round count is almost entirely a
function of how much you pre-checked.

Observed round counts: SRS-LOG-001 **38**, DATA-018 **23 findings / 15 rounds**, REL-001
**20**, DATA-007 **17**, RESV-003 **14**, API-001 **14**, DATA-012 **14**, SAFE-002 **13**,
DATA-015 **13**, UI-5 **13**, ORCH-005 **10**, NOTIF-001 **9**, MD-003 **9 + 4 live**.

## Rule 0 — fix the CLASS, not the instance

**On any BLOCK, before you re-request review:**

1. **Sweep.** Grep every peer call site, sibling surface, and contract block for the same
   defect. The reviewer will.
2. **Fix them all in this round.** Not "the one it named."
3. **Write the guard.** Add a static collector or a test that fails for the whole class —
   not for the single instance.

Why: LOG-001 spent ~20 of 38 rounds re-finding six recurring classes at the next call site.

| Class | Re-found at |
|---|---|
| absent/missing store not failing closed (TOCTOU) | r7, r10, r20, r25 |
| readiness claim lifecycle (claim / release / stop / restart) | r14, r15, r17, r22, r23, r28 |
| stale "deferred / contract-only" prose after the surface shipped | r11, r19, r29, r33 |
| unbounded O(whole-trail) read | r4, r24, r36 |
| value used as identity instead of physical identity | r5, r16, r18 |
| declared contract vs emitted response drift | r2, r6, r8, r15, r22 |

The session's own words at r29: *"when the same statement is duplicated across contract
blocks, fixing instances one at a time does not converge — write the check."* It then
immediately caught a third instance the manual sweep had missed. `(LOG-001 r29)`

**But a guard shaped like a CHECKLIST cannot catch the arm nobody added to it.**
RESV-006 wrote the check — and wrote it as "these two methods consult the predicate". It
passed happily while a third entry point demoted and promoted with no cool-down at all,
because the two it named only *minted a proposal* and nothing in the type graph required a
swap to have come from one. Enumerate the arms **from the source**: find the functions that
reach the dangerous side effect and require the guard on each, with per-item exemptions that
carry a stated reason. The rewrite immediately found a fourth arm nobody had listed. Then
prove the discovery itself: markers that match nothing must FAIL rather than pass vacuously,
and a known arm vanishing from the scan must fail too — a broken discovery reports a clean
tree, which is the failure mode that looks most like working. `(RESV-006 r2)`

## When a guard keeps failing, stop describing and start bounding (SRS-MD-005 r2-r6)

**A guard that DESCRIBES the dangerous code cannot close a class. Move the code
until the language closes it for you.** SRS-MD-005 needed "every path that can
admit a market-data subscription consults the restart window", and the check was
rewritten four times, each version passing a bypass the reviewer wrote in one
line:

| version | discovery rule | walked past by |
|---|---|---|
| r2 | functions that already take the gate port | a path that skips the port — i.e. the whole point |
| r3 | two literal effect forms | `subscribers.entry(k).or_default().push(..)` |
| r4 | public `&mut self` on the inherent impl | a trait impl, and a free function in the same file |
| r5 | functions in `lib.rs` naming the private field | a CHILD module: Rust exposes privates to descendants |

Every one was a better description and none was a closure. The fix that ended it
was not a fifth scan — it was moving the registry into its own module, so the
field is unreachable from the crate root and every sibling. Then "the functions
in that file" is a set the COMPILER closes, two `compile_fail` doctests prove
the boundary, and the check verifies the boundary still holds rather than
asserting a language property. `(SRS-MD-005 r2-r6)`

Three corollaries worth having before you write the guard:

- **Ask what the compiler already guarantees**, and put the code where that
  guarantee is the one you need. A module boundary, a private field, or a
  `pub(crate)` constructor is worth more than any regex over the same tree.
- **Rust privacy is parent-to-child, never child-to-parent.** A private field in
  `lib.rs` is visible to every `mod` declared there. "Private to the file" is
  only true for a file that is not an ancestor.
- **A guard that scans source will flag its own documentation.** This bit three
  times in one feature: `to_socket_addrs` inside a doc comment saying "never
  call `to_socket_addrs`", the string `4001/4002` inside "never touches
  4001/4002", and a `compile_fail` doctest declaring `fn sibling_reach` that
  names the very field the scan forbids. Strip comments before scanning, and
  keep a non-vacuity assertion that the stripper did not eat the code.

`(SRS-MD-005)`

## The pre-check list

Walk this against your own diff before Step 6.1. Most of it is one grep each.

1. **Every entry point, not the outermost.** A rule enforced in the REST wrapper leaves the
   CLI, the Rust API, and the direct library call able to violate it. Put the guard where
   all arms pass through, and enumerate the arms. `(RESV-003 r2; EXE-003 r3/r4)`
2. **Unreadable / absent / unknown ≠ empty.** Give each its own error type and its own
   rendered state. A missing file *reads successfully* and yields zero records — the exact
   shape of a healthy quiet system. `(LOG-001 r7/r10/r20)`
3. **No TOCTOU pre-check.** `if exists(): open()` is a race. Let the absence surface from
   the open itself; keep `exists()` only as a nicer-message fast path. `(LOG-001 r20/r25)`
4. **Value is not identity.** Two byte-identical records are two events — a retried
   operation writes the same message with the same correlation id. Key cursors and dedupes
   on physical identity (device+inode+offset), and verify content *as well as* position
   because inodes and offsets get reused. `(LOG-001 r5/r16/r18)`
5. **Bounded reads.** Any read whose size grows with history is a defect on a polled path
   and a hazard on a request path. State the trade if you keep an O(n) scan, and re-check
   whether a poller shares that code. `(LOG-001 r4/r24/r36)`
6. **Declared contract == emitted surface, both directions.** A superset is undeclared
   drift; a subset is an unkept promise. Serve a real request in the test and compare the
   full field set, per level (top-level and nested items separately). `(LOG-001 r6/r8)`
7. **Implemented ≠ shipped.** Drive the e2e through the *shipped* composition helper, not a
   test fixture that mounts the handlers itself. `serve()` never mounting the routes is a
   real, shipped bug that every test can miss. `(RESV-003 r7)`
8. **Writer must satisfy its own reader.** If you harden a parser, re-check the emitter:
   a hand-rolled escaper that covers five characters will now poison its own log.
   `(RESV-003 r3; DATA-015)`
9. **Order of operations around durable writes.** Do every fallible read *before* the
   append. Counting after appending means the caller is told "did not happen" while the
   disk says it did. `(RESV-003 r4)`
10. **Concurrency where you assumed a single writer.** The HTTP server is threaded. A
    comment claiming serialization is not serialization. `(RESV-003 r5)`
11. **Same-named exception in two modules.** The `except` clause misses the foreign class
    entirely. Exception identity must be one object; assert `is`. `(RESV-003 r6)`
12. **Scope your own claims.** After hardening, re-read the prose you wrote: module docs,
    the check tool's printed evidence string, `deferred[]` entries, the README. A claim
    that outruns the code is its own BLOCK. `(EXE-003 r1/r3/r5; UI-4 r6)`

## Handling the reviewer

- **A TIMEOUT is not a verdict.** `{"verdict":"block","findings":[]}` with a timeout summary
  is an availability failure. Retry — on a ~40-file diff the fallback timed out twice and
  the third attempt found a real BLOCK. Never `--base`-shrink the diff to make it finish.
  `(LOG-001 r38)`
- **An empty-summary `claude-fallback` APPROVE is a dropped verdict**, not an approval.
  A real verdict has a populated summary and findings. `(DATA-011)`
- **A verdict that flips to approve with no diff change is unresolved**, not an approve.
  `(DATA-018)`
- **Its observation can be right while its recommendation is wrong.** Twice on DATA-018 the
  literal advice would have broken something. Fix the root cause it points at, and say in
  the note where you diverged and why. `(DATA-018)`
- **Some loops structurally cannot converge** — see
  [scope-and-serialization.md](scope-and-serialization.md) for how to stop honestly.
- **A reviewer can be silenced by the code it is reviewing.** The telemetry branch's
  `outcome()` classified any reply containing "usage limit" as an outage; every review OF
  that file quoted the phrase, so round 7's real BLOCK was recorded as an attempt with
  **zero findings** and the round count went to 0. Exit stayed 1, so nothing looked wrong.
  When your diff touches the review path itself, read the LEDGER (`kind`, `n_findings`,
  `count_rounds`) against the verdict you were shown — they disagreeing is the tell.
  `(harness-p1 r7, found by the reviewer falling into it)`
- **A heuristic must never overrule a parsed answer.** Substring scans, filename guesses and
  "looks like an error" checks are for the case where you got NOTHING. The moment a real
  answer parses, the guess must yield — otherwise the fallback path eats the happy path.
  `(harness-p1 r7)`
- **Consolidating N call sites into one predicate re-opens every case at once.** Rounds 5-7
  each fixed the previous round's fix: the class fix covered 4 of 7 sites, the consolidation
  that followed was too eager and discarded real verdicts. After collapsing sites into one
  decision, parametrize BOTH directions — the cases that must be caught AND the ones that
  must still pass — or the new predicate buys its precision with false positives nobody
  tested for. `(harness-p1 r5-r7)`

## Before you call the review clean

- Deterministic critic APPROVE on the staged set.
- Every fix from every round is mutation-verified.
- The last SUBSTANTIVE round covers the tree you are shipping. If it does not (the operator
  closed the session, or the reviewer was rate-limited after your last edit), say so
  explicitly in the session note and name which round the last real pass covered.
  `(LOG-001 r38 note)`

## When one defect class keeps coming back (RESV-006, r13→r21)

- **Five rounds found the same family**: a durable record with two slots that can each be
  "the current one", and each round found an arrangement the previous fix had not covered
  (confirm clobbers, abandon deletes, retry deletes, confirm shortens, a second attempt
  displaces). Patching instance N+1 is not convergence. **Enumerate the writers in the
  contract, then write the closure argument**: for each, state how it either preserves or
  extends the invariant, and add the static check that fails when an undeclared writer
  appears. An argument over a closed set is checkable; "the cases we thought of" is not.
- **Ask the requirement which combinations are LEGAL, and test those first.** Every one of
  those five landed on a sequence the requirement explicitly permits — SYS-49a(a) allows a
  manual swap DURING the window the feature enforces, so two records coexisting is normal
  operation, not an edge case. The permitted-concurrent-operation path is where this class
  lives. `(RESV-006 r21)`
- **A fix that spans a store and its surface is not done until BOTH ends compare the same
  thing.** RESV-006 hardened the store's ownership check (r26) and left the operator command
  matching on the old, weaker key and then acting with the value it had just re-read — which
  turned a compare-and-swap back into read-and-clobber while every commit message said
  otherwise. When you strengthen an identity, grep every caller that *names* that identity
  and check what each one COMPARES, not just what it passes. `(RESV-006 r27)`

- **A character class you wrote to bound a generic list will terminate on the `>` of a
  `->` return arrow.** `impl(?:<[^>]*>)?\s+Trait\s+for\s+(\w+)` cannot match
  `impl<C: Fn() -> i64> Trait for AlwaysOpen` - and an injected clock closure is exactly
  the idiom this codebase uses, so the guard was blind to the shape it would actually
  meet. Bound the span by what CANNOT appear in it (`[^{};]*?`) rather than by the
  delimiter you expect to close it. Then inject the shape and watch it get caught.
  `(SRS-MD-005 r14)`
- **A scan whose subject list is hard-coded is bounded by the day it was written, not by
  the tree.** The gate-implementor enumeration listed the four crates that had the trait
  in view; the contract beside it claimed it "walks the crate sources". A fifth crate
  gaining the dependency later would be unscanned with every guard still green. Glob the
  tree, and `fail()` when the glob returns nothing - a scan that finds nothing must be
  red, never a clean report. `(SRS-MD-005 r14)`
- **Make a guard's exemption self-expiring, not asserted.** `cmd_gate` legitimately need
  not refresh the rendered page - because the renderer emits no gate state. Writing that
  reason in a comment leaves the exemption silently wrong the day gates reach the page.
  Assert the *condition that justifies it* (`"gates" not in inspect.getsource(render)`),
  so the exemption fails the moment its premise does. `(SRS-MD-005 r14)`

- **An impl target is a TYPE, not an identifier.** `for\s+(\w+)` misses
  `impl Gate for &AlwaysOpen`, `impl<'a> Gate for &'a AlwaysOpen` and
  `impl Gate for (AlwaysOpen, u8)` - all legal Rust, all production implementors, all
  invisible to a "closed set" enumeration. Capture the span up to `where` or `{`, strip
  its generic arguments (or `P` and `C` in `Foo<P, C>` each read as a type and the guard
  cries wolf on the declared producer), then take the type names. `(SRS-MD-005 r15)`
- **When you fix a regex defect, grep the file for the same shape before you write the
  playbook entry about it.** The `[^>]*` fix landed 54 lines below two siblings carrying
  the identical class, one of which would have `fail()`ed claiming the producer did not
  implement the trait the moment it grew an `Fn() -> i64` bound. `(SRS-MD-005 r15)`
- **A "class" guard that keys on a DIRECT call sees only the odd path.** The recorder
  check looked for `save_record` by name, which caught the two commands that call it
  directly and missed `run`, `record` and `artifact` - the three that persist through
  `_store_step`, i.e. the normal way to write one. Close the relation transitively and
  assert the closure reaches the known helper, so the walk itself is tested.
  `(SRS-MD-005 r15)`
- **Scope a cross-file consistency check to the LINE, not the file.** Comparing every
  "N rounds" claim in the queue against every feature id in the queue produced seven
  false accusations on its first run. A row is one line; keep the claim and its subject
  together. `(SRS-MD-005 r15)`

- **A `->` will defeat your bracket matcher. Again.** Three separate patterns in one
  feature: `(?:<[^>]*>)?` for an impl's generics (r14), `for\s+(\w+)` for its target
  (r15), and `<[^{}();]*?>` for a `fn` declaration's generics (r17, where excluding `(`
  broke on `<F: Fn() -> bool>`). Write ONE named pattern for "a generic list" that admits
  `->` explicitly, use it everywhere, and test it against a bound containing both a
  parenthesis and an arrow. `(SRS-MD-005 r17)`
- **A gate that is real is not automatically a gate that is RELEVANT.**
  `cargo build && echo 'builds clean'` is honest; `ls && echo '176 suites ok'` is not, and
  a check that only asked "is the gating command a no-op?" accepted both. Require the
  claim and its gate to be about the same thing - at least one substantial word of what
  is printed appearing in the command that gates it. `(SRS-MD-005 r17)`

- **When the same defect class appears a fourth time, stop patching call sites and go
  count them.** A `->` defeated four separate patterns in this one feature: impl generics
  (r14), the impl target (r15), a `fn` declaration's generics (r17), and the manual depth
  counter in `_strip_generic_args` (r18), which decremented on the `>` of the arrow and so
  reported a RETURN TYPE as an undeclared production implementor. Each fix was correct and
  each round found the next one, because the fix was always local. `grep -n '\[^>\]\|>' `
  over every pattern in the file, once, would have ended it four rounds earlier.
  `(SRS-MD-005 r18)`
- **Test the false-positive direction of a guard, not only the bypass.** A guard that
  `fail()`s on a legal shape gets disabled by the next person who meets it, so it is as
  dead as one that never fires. Every bypass test here now has a sibling asserting the
  legal shape stays quiet. `(SRS-MD-005 r18)`

- **Stop writing regexes for nested syntax; count.** Bounding a Rust generic list by
  pattern failed four times in a row on shapes each version had not anticipated: a `->`
  closed it early, then a parenthesised bound, then `<T: Into<Vec<u8>>>` exceeded the one
  nesting level the regex allowed. A bracket counter has no depth limit and needs no
  alternation for the arrow, which is simply "a `>` whose predecessor is `-`". Twenty
  lines, and the class is closed. `(SRS-MD-005 r19)`
- **A guard keyed on a NAME is defeated by a rename.** `use atp_market_data::RestartWindowGate as Gate;`
  then `impl Gate for AlwaysOpen` produced no match, while the check went on printing
  "this enumeration is what makes it unforgeable". Collect the file's `use ... as` aliases
  (braced groups too) and search for every name the trait answers to.
  `(SRS-MD-005 r19)`

- **A backstop bounded like the pattern it backs up is not a backstop.** The completeness
  check written to end five rounds of "your regex missed this shape" counted with `[^;]`,
  the same boundary the strict pattern used - so any shape a `;` defeated defeated BOTH,
  and `expected == matched == 0` read as a clean, closed set with an always-admitting
  implementor sitting in it. A backstop must be bounded by something the thing it guards
  genuinely cannot contain (here: braces), and it must be TESTED against a shape the
  strict pass fails. `(SRS-MD-005 r21)`
- **A hand-written parser must return "unparseable", never "nothing".** Replacing the
  regex with a bracket counter fixed the depth problem and introduced a worse one: on a
  `;` inside `<T: Into<[u8; 4]>>` it returned the start index, so the caller silently
  DROPPED that declaration and the exemption was inherited by a function the scan could
  not read. Return a sentinel and make the caller refuse. Failing open is the one outcome
  a guard may never have. `(SRS-MD-005 r21)`
- **A rename can be two hops.** Collecting `use ... as` aliases from the file being scanned
  missed `pub use Trait as Gate;` in one module followed by `use crate::gates::Gate;` in
  another - no strict match, no loose match, clean report. Collect aliases across the whole
  tree and close over them, because an alias can itself be renamed. `(SRS-MD-005 r21)`

- **A bounded closure that has not converged has not answered the question.** The
  trait-alias closure added to end the two-hop rename bypass ran a fixed number of passes
  and then fell through and scanned with whatever it had - the exact fail-open shape that
  had just been removed from the parser beside it. If the set is still growing when the
  budget runs out, `fail()`. `(SRS-MD-005 r22)`
- **Check what your error MESSAGE degenerates to when you build a pattern from it.** The
  queue disclosure required the blocked layer's name, taken as `blocked[0].split()[0]`. For
  a record with no critic block at all the entry was the sentence "no critic block
  recorded", so the required word became the bare `no` - the most fail-open record
  produced the weakest check. Build patterns from identifiers, never from prose.
  `(SRS-MD-005 r22)`

- **When you correct a claim, grep for its PEER SURFACES in the same commit.** The
  verification queue's "close over a standing block" route was removed as having no
  mechanism; the identical false route stayed in the session note's Resume block and was
  found one round later. Same class, same feature, two files. `grep` the corrected phrase
  across `docs/`, `progress.d/` and the rustdoc before you call it fixed - this feature
  paid for that lesson three separate times (r18 the Outcome line, r23 two of five
  invariant copies, r24 the Resume block). `(SRS-MD-005 r24)`

- **Naming a bypass in a comment is not closing it.** The typed-result guard's own comment
  listed three shapes it had closed - `sys.stdout.write`, a here-string, and a `cat <<EOF`
  heredoc - and the code closed two. The heredoc stayed open for two more rounds behind a
  comment claiming otherwise, which is worse than no comment: a reader checking the guard
  would have stopped at the list. If you name a shape, add its test in the same edit.
  `(SRS-MD-005 r25)`
- **Fix a defect in every function of the file, not just the one under review.**
  `_queue_row_problems` was taught to assemble wrapped markdown ROWS; `_round_count_drift`,
  one function below it in the same file, kept reading LINES and skipped a stale total on
  a continuation line. The row assembly is shared now. When you fix a parsing defect, grep
  the FILE for the same read pattern before you leave it. `(SRS-MD-005 r25)`
