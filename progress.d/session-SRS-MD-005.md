=== SESSION SRS-MD-005 ===
Date: 2026-09-04
Feature: SRS-MD-005 — handle the scheduled IB Gateway daily restart as planned maintenance
Outcome: serialized (A: done — every step ran solo and is recorded; the close needs
         an operator attestation because verification_method is `integration`)

## Why this feature was only ever half-built

Every CONSUMER of the restart window already existed and had for months:
`ConnectivityState::ScheduledRestartWindow` in atp-types, the execution engine's
live-order gate refusing on it, the SRS-NOTIF-001 dispatcher suppressing
connectivity alerts for it, the dashboard marker. What did not exist was the
thing that DECIDES when the window is open. `atp-notification`'s own module docs
said so — "the restart-window decision is owned by SRS-MD-005; this enum is the
seam the dispatcher honours" — and SRS-SAFE-003 recorded `block --on SRS-MD-005`
for the same reason, having verified that every `impl BrokerageConnectivity` in
the tree was a test fixture.

So this session built the producer, and it is the first non-fixture
`BrokerageConnectivity` in the repo.

## What I did

* **atp-types** — `RestartWindow`: validated, clock-free, private fields, no
  `Default` (a window that materialised from nowhere would suspend trading on a
  schedule nobody chose). It classifies an injected epoch-ns instant into
  `RestartPhase {Normal, Suspending, Restarting, Elapsed}` and maps that onto
  `ConnectivityState` with an exhaustive match and **no catch-all arm**, so a
  phase added later fails to compile rather than inheriting whichever answer was
  permissive. `MarketDataAdmission` carries the REASON, not just the decision.
  Plus `StructuredSubscriptionError::{suspended_for_scheduled_restart,
  connectivity_lost}`.
* **atp-market-data** — `RestartWindowGate` is a required PORT (not a
  caller-supplied bool, which is forgeable) on both subscription admission
  points. The guard runs ahead of the ERR-4 limit match, so those pinned arms
  stay byte-identical. The registry MOVED to `src/subscriptions.rs` — see the
  guard story below; that move is load-bearing, not tidiness.
* **atp-adapters** — `gateway_reachability.rs`, a bounded literal-`SocketAddr`
  TCP probe in its OWN module. `interactive_brokers.rs` and its `wire.rs` are
  SHA-256 pinned by the SRS-EXE-006 live evidence and were not touched
  (`ib_adapter_check` green throughout).
* **atp-orchestrator** — `ScheduledRestartConnectivity` composes window + probe +
  injected clock and implements BOTH `BrokerageConnectivity` and
  `RestartWindowGate`, so the order gate and the market-data gate cannot
  disagree about an instant. Plus the end-to-end scenario and
  `md005_connectivity_restart_window_cli`.
* **python/atp_orchestration/restart_schedule.py** — resolves 23:45 ET through
  the DST-aware `atp_strategy.calendar` authority. Python resolves, Rust
  classifies: the Rust workspace has zero third-party crates and therefore no
  timezone database, so implementing DST there would fork a calendar the repo
  already has, and a missed adjustment would move the window by an hour.
* **SRS-ARCH-005** — three catalogued keys across the catalogue, `.env.example`,
  `init.sh`, the config README and two negative fixtures wired into CI.
* **Contract** — `connectivity_contract.restart_window` plus seven new static
  guards in `connectivity_check.py`, each with its own mutation test.

## The decision that took the longest: what "closed set" means

The requirement "every path that can admit a market-data subscription consults
the window" needs a guard over a set a checker can BOUND. Four attempts failed
in review, each a better DESCRIPTION of the dangerous code and none a closure:

| round | discovery rule | reviewer's one-line bypass |
|---|---|---|
| r2 | functions that already take the gate port | a path that skips the port — the whole point |
| r3 | two literal effect forms | `subscribers.entry(k).or_default().push(..)` |
| r4 | public `&mut self` on the inherent impl | a trait impl; a free fn in the same file |
| r5 | functions in `lib.rs` naming the private field | a CHILD module — Rust exposes privates to descendants |

r6 stopped writing scans and moved the code. `ConsolidatedSubscriptionRegistry`
now lives in `crates/atp-market-data/src/subscriptions.rs`; privacy runs
parent-to-child and never child-to-parent, so the crate root and every sibling
are UNABLE to name `subscribers`. Verified by probe (`error[E0616]`), proved by
two `compile_fail` doctests (mutation-verified: make the field `pub` and both go
red), and the guard now re-checks the boundary instead of asserting it. That is
the lesson written back to `adversarial-precheck.md`.

## Key decisions

* **The lead suspends regardless of reachability.** SYS-75(a) is pre-emptive —
  the gateway is by definition still up 60 s before its own restart, so a rule
  derived from reachability could never fire. The producer also skips the probe
  there: the gateway serves ONE API client, and asking would spend the slot the
  reconnect is waiting for.
* **`Elapsed` + unreachable → `Unreachable`, not the window state.** This is the
  clause the whole requirement turns on: `Unreachable` carries
  `scheduled_restart:false` into `ConnectivityEvent`, which is what makes
  `suppression_for` page instead of suppress. A window that never closed would
  silence a real failure indefinitely and every other test would still pass.
* **Only an UNREACHABLE observation is reused; the phase never is.** The
  execution engine consults this port INLINE on the live submission path, so the
  probe deadline is spent inside NFR-P1's 1,000 ms order budget, not beside it
  in NFR-R2's 15 s. Probe bounded to 250 ms, with a 1 s reuse window — and the
  asymmetry is the design. Caching a POSITIVE would mean that for up to a second
  after the gateway died, `state()` still said `Connected` and the ERR-2 gate
  handed a live order to a dead gateway rather than refusing: a safety property
  traded for latency, in the one direction this feature must never move. It is
  also unnecessary, since a successful connect is microseconds; the expensive
  case is precisely the unreachable one, and reusing it errs toward BLOCKING.
  The phase is recomputed on every read because the two instants that matter are
  exactly where a cached verdict is wrong.
* **A refusal states WHICH refusal.** Inside the window "suspended" tells the
  operator to wait; after it, the identical refusal is an incident. One boolean
  would have told them to wait out an outage.

## What I deliberately did NOT do

* **Did not widen the ERR-2 order envelope.** `submit_live_order` still reports
  `error_type: "IbGatewayUnreachable"` for both blocked states, so during the
  lead the order surface and the market-data surface word one instant
  differently. It is pinned by the closed-green ERR-2 contract and its tests;
  layering on top and recording the divergence is the honest move. In
  `restart_window.deferred[]`.
* **Did not build the continuous connectivity watcher.** SRS-EXE-001's named
  scope. This producer answers when asked.
* **Did not fold readiness into `Connected`.** A reachable probe means the
  gateway accepts TCP, not that the API answers a handshake. ERR-9 /
  SRS-MD-006 own that.

## What I tested (per feature step)

* **Step 1** — PASS. `./init.sh` → `✓ Environment ready` (17/17 env contract
  checks). Re-run after the worktree refresh described below.
* **Step 2** — PASS. `cargo test -p atp-orchestrator --test
  srs_md_005_restart_window_cli` → 15 passed. Drives the real binary in fresh OS
  processes over the real `dispatch_order → route_order → submit_live_order`
  chain, the real subscription manager AND registry, the real SRS-NOTIF-001
  dispatcher, and a real TCP probe against a real loopback endpoint.
* **Step 3** — PASS, all four AC clauses:
  - suspension 60 s before: `prove-suspension` → state `ScheduledRestartWindow`,
    order `CONNECTIVITY_BLOCKED`, `ib-orders-created:0`, market-data refused,
    `registry lines-opened:0`;
  - suppression: `alerts disposition:SUPPRESSED messages-sent:0`;
  - reconnection attempted: `reconnects:1`, and `prove-resume` shows a gateway
    that returns inside the window resuming (`ib-orders-created:1`,
    `lines-opened:1`);
  - escalation: `prove-escalation` → `Unreachable`, `scheduled_restart:false`,
    `disposition:DISPATCHED messages-sent:2`, `admission:CONNECTIVITY_LOST`.
  Every one paired with its non-vacuity control (`--inject`), all of which fail
  closed with no proof line.
* **Step 4** — PASS. `ATP_RUN_INTEGRATION=1 pytest
  tests/integration/test_md005_restart_fault_injection.py` → 8 passed. Fault
  injection against a genuinely dead loopback port, never 4001/4002. Run as the
  SOLE lease-holder (`tools/.agent_runtime.json` showed one lease).
* **Full gate** — `cargo test --workspace` 176 suites ok; `cargo clippy
  --workspace --all-targets -- -D warnings` clean; `cargo fmt --check` clean;
  `pytest -m "not integration and not e2e"` 5370 passed; `tools/run_ci_locally.sh`
  → `✓ local CI mirror complete — every step ran` (mypy advisory, pre-existing
  in `atp_orchestration/hot_swap_triggers.py`, not mine).

## Mutation verification

`tools/mutation_verify.py origin/main..HEAD` (venv interpreter): all 52 added
Python tests go red without the change. It counts Python only, so all **9 Rust
properties were hand-mutated**, one property per mutation, each required to kill
exactly its named test: the pre-emptive lead, the escalation, the phase
boundaries, both refusal labels, the mutating admission point, the shared
probe-skip on both gates, the published-flag read, and the shared window. Plus
the two `compile_fail` doctests.

**A trap worth knowing:** restoring a mutated file with `shutil.copy2` preserves
the mtime, so cargo kept serving the MUTANT artifact and the next run reported
its behaviour as the restored code's. Two "defects" were chased before the cause
surfaced. Written back to `test-integrity.md`.

## Critic verdicts

  deterministic (tools/critic_check.py --staged): APPROVE — no findings, on
  every one of the nine commits. It BLOCKed twice mid-session for the right
  reason (a safety-path diff without a paired `tests/domain/` test) and both
  times the fix was a real pin, not a token.

  judgment (tools/adversarial_review.py, reviewer=claude-fallback): **BLOCK at
  round 13 — the loop did not reach APPROVE, and this integrates on OPERATOR
  AUTHORIZATION, not on a green verdict.** The operator stopped it at round 13
  with "Close out. You are running in a loop." That call is recorded here rather
  than smoothed over, and no APPROVE was faked. `evidence.py verify` reports
  `evidence INCOMPLETE — judgment critic verdict is 'block'`, which is the
  correct machine state and is why this integrates `serialized`.

  **Why stopping was right, and what it costs.** Rounds 1-4 found defects in the
  FEATURE. Round 5 found the sharpest one (a 2 s blocking probe on the live-order
  path, bounded against the wrong requirement). From round 5 onward the findings
  were increasingly about the static GUARD TOOLING rather than the restart-window
  behaviour: five successive holes in one check, each fix inviting the next probe
  of the same surface. That is `adversarial-precheck.md`'s "same defect class
  keeps coming back" signal, and `scope-and-serialization.md` rule 9's
  non-convergence shape. The feature's own acceptance criteria have been green
  and mutation-verified since round 5.

  **The residual, stated plainly.** Round 13's findings were all addressed (the
  gate-implementor enumeration, the test-module stripper, the precedence
  residual, the readiness label), but they were never re-reviewed — no round ran
  against the tree being shipped. A fresh round would very likely find more in
  `tools/connectivity_check.py`. What that check enforces is a SECOND layer: the
  property it guards is already enforced by the compiler (the registry's
  `subscribers` field is private to its own module, proven by E0616 and two
  mutation-verified `compile_fail` doctests). A hole in the checker is not a hole
  in the suspension.

  **Codex never produced a parseable verdict in this environment** — all 14
  Codex attempts recorded `codex output unparseable`, so the fresh-context
  Claude reviewer carried every round. Per `prompts/critic_prompt.md` that is a
  first-class path, not a degraded one, but the Codex leg being down on this
  machine is worth an operator's attention independently of this feature.

Adversarial rounds: 13 (plus no-verdict attempts, one a fallback TIMEOUT that
was retried rather than treated as a verdict — an availability failure is not a
BLOCK, and shrinking the diff with --base to make it finish is forbidden).

  r1  block/10 — the guard was circular (discovered admission sites by the port
      they already took); MSRV violation (post-1.75 ErrorKind); the market-data
      gate did not inherit the order gate's probe-skip; two false claims about
      shared wording; an empty env value silently defaulted.
  r2  block/7  — the clock was read twice around a blocking probe, so the two
      gates could classify one moment into different phases; the EVIDENCE path
      probed during the lead, breaking the invariant it reported on; two new
      config fixtures ran nowhere.
  r3  warn/3   — same class, narrower.
  r4  block/5  — the guard, second shape: two literal effect forms, walked past
      by entry().or_default().push(); ProbeFailed misattributed as an IB outage;
      the catalogued keys validated and changed nothing; the reconnect ledger
      grew without bound.
  r5  block/4  — the guard, third shape; and the sharpest finding of the
      session: a 2 s blocking probe on the live-order path, argued only against
      NFR-R2's 15 s reconnect budget while it is actually spent inside NFR-P1's
      1,000 ms order budget. Also: prove-suspension printed its proof for a
      phase it had never entered.
  r6  block/4  — the guard, fourth shape: "the functions in lib.rs" is not a
      closed set, because Rust exposes privates to DESCENDANTS. Fixed
      structurally (module move) rather than with a fifth scan.
  r7  block/3  — stale contract rationale; a unittest entry point ahead of the
      new classes; `subscribe` absent from the integration evidence.
  r8  —        — TIMEOUT, no verdict. Retried.
  r9  block/5  — exemptions keyed by bare NAME could be inherited by a new
      function; a submodule would go unscanned; a doctest documented as proving
      the intra-crate case actually proved the external one; an overflow
      panicked instead of refusing. That last fix found a gap the reviewer had
      not: `--restart-ns 0` put the suspension before the epoch.
  r10 block/5  — the comment stripper deleted every line starting with `*`, so a
      deref-assignment admission point was invisible; the "no ADD" exemption
      check was evaded by a local alias; and the one that mattered: the
      reachability cache could report `Connected` for up to a second after the
      gateway died, so the ERR-2 gate would hand a live order to a dead gateway.
      Only NEGATIVE outcomes are cached now.
  r11 block/4  — `pub(crate)` sailed past the privacy gate (the regex matched a
      bare `pub` only) and re-opens the field to every sibling module; the
      crate-root half of the guard read its source without stripping comments,
      so a COMMENT mentioning the guard call satisfied it, and that half had no
      mutation test at all.
  r12 warn/3   — the first round with no BLOCK, and it still found a real
      operator-facing defect: `subscribe` consulted the window BEFORE
      canonicalizing, so during the window an option / empty symbol / empty
      strategy id came back as "planned maintenance, retry after the window" —
      false for a request that can never succeed. Validation now runs first.
      Plus: the interior-mutability ban named three shapes while claiming the
      class, and `last_outcome()` promised a freshness it did not enforce.
  r13 block/3  — the port's rustdoc claimed taking a port closed a FORGERY hole;
      it does not (an impl returning Admitted is as forgeable as `true`, and a
      production one would bypass SYS-75(a) with every guard green). Closed by
      enumerating production implementors — which immediately exposed a second
      hole: the test-module stripper truncated at the marker, so production code
      after a test module was invisible to every scan built on it. Also, fairly:
      CLAUDE.md rule 1 — round 12 fixed the precedence defect at one admission
      point and left it at the peer with the residual only in a commit message.
      And prove-resume's sentinel named the whole SYS-75(c)/(d) clause while
      resting on a bare TCP accept. All addressed; NOT re-reviewed.

Every finding was fixed; none was overridden or argued away. Where a finding's
recommendation would have been wrong I did not diverge — all nine were correct
as stated.

## Playbook updates

  docs/playbooks/adversarial-precheck.md — "When a guard keeps failing, stop
    describing and start bounding", with the four-round table and the three
    corollaries (ask what the compiler already guarantees; Rust privacy is
    parent-to-child; a source-scanning guard will flag its own documentation —
    which happened three times in this feature alone).
  docs/playbooks/test-integrity.md — mutation harnesses that lie (mtime-
    preserving restore leaves cargo serving the MUTANT; an anchor moves when a
    module is extracted; compile_fail doctests as cheap encapsulation proof),
    and evidence that breaks what it reports on (the reporting path violating
    its own invariant; a proof line that outruns the phase it names; evidence
    that re-derives instead of reading; a --fixture nobody runs).
  docs/playbooks/safety-paths.md — a gate on the order path is inside the
    order's latency budget; cache the sampled FACT, never the derived STATE; a
    refusal must say WHICH refusal it is; a configured knob that changes no
    behaviour.
  docs/playbooks/contract-drift.md — a contract that names a FILE breaks when a
    module is extracted.
  docs/verification-queue.md — SRS-MD-005 added as Class A.

## Notes for the operator

* This worktree was **530 commits behind `origin/main`** at session start —
  fast-forwarded before any work. Worth checking at claim time.
* `feature_list.json` `notes` cannot be edited from a branch (no tooling path
  writes it). Nothing needs changing there for this feature.

## Resume / next — to flip passes:true

All four steps pass and are recorded in `.harness/runs/SRS-MD-005/evidence.json`
with real commands and real exit codes. TWO things stand between here and green,
and only one of them is work:

1. **The judgment verdict is `block` at round 13**, so `evidence.py verify`
   refuses. Whoever closes this decides between two honest routes:
   * re-run `python3 tools/adversarial_review.py origin/main` and address what
     it finds — expect more in `tools/connectivity_check.py`, which is a SECOND
     layer over a property the compiler already enforces; or
   * close on the same operator authorization that stopped the loop, recording
     that the judgment layer did not reach APPROVE on guard-tooling scope. The
     precedent is `docs/playbooks/scope-and-serialization.md` rules 9-12.
2. **A named attestation**, because `verification_method` is `integration`. From
   the PRIMARY checkout:

       python3 tools/close_feature.py SRS-MD-005 --verified --attested-by operator

Nothing is outstanding for the acceptance criterion itself. The four items in
`connectivity_contract.restart_window.deferred[]` are follow-on scope owned by
other features (the daily roll-forward and `ATP_IB_RESTART_ET` wiring with
SRS-EXE-001's connectivity daemon; the ERR-2 envelope wording and the probe
failure reason with whoever owns that contract; NFR-R2 enforcement with the
wire-level reconnect), and SRS-SAFE-003 can drop its `--on SRS-MD-005` edge once
this flips.
