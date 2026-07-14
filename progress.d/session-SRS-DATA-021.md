# SESSION — SRS-DATA-021 (apply corporate actions to paper virtual positions and orders)

Outcome: serialized

## What landed

- **atp-data — the shared fact seam**: `CorporateActionFact` +
  `MarketDataStore::query_corporate_action_facts` (coverage.rs). The
  coverage-gated, point-in-time corporate-action FACT read: split + dividend
  TERMS (with the reference close resolved from the raw lineage series) plus the
  structural delisting/merger/symbol-change events, window-bounded
  `[start_ts, end_ts]`, no lookahead, same kind guard / coverage gate / lineage
  resolution / crate-internal extractors as the six adjusted reads. This is the
  "same corporate-action data source" seam — the SRS-DATA-019/020 composition
  roots should map THIS read at their (deferred) wiring, not grow parallel
  record parsers.
- **atp-simulation `virtual_orders`**: the paper resting-order store
  (`VirtualOrderBook` / `VirtualRestingOrder` / terminal
  `VirtualOrderStatus::Cancelled{reason}`). SRS-SIM-001 intake never retained
  an order; the SIM-004 snapshot reserved an always-empty slot for exactly this
  store. Placement goes through `paper_order::validate_leg` (made `pub(crate)` —
  one intake authority). Persisting the book into SIM-004's reserved slot and
  fill-path consumption of resting orders remain those owners' work.
- **atp-simulation `corporate_actions`**: `apply_corporate_action` /
  `apply_and_emit` transform the books IN PLACE (paper owns its state — the
  deliberate delta vs the plan-only live siblings). Position math is
  semantically byte-stable with SRS-DATA-020: split = exact N/M qty scale +
  INVARIANT total basis (signed-safe shorts); dividend = ADDITIVE
  `basis − amount·qty` (sign-crossing → review); merger/symbol-change = remap
  with realized P&L + full cost decomposition carried intact, same-strategy
  successor collision (incl. flat-record history) → review; delisting =
  `DelistedHold` report + page (no VirtualPosition status field — the order
  cancel is the actionable half). Orders: delisting/merger → terminal cancel
  (merger cancels regardless of term validity — the series terminates); split →
  rebase qty exact-positive + prices half-to-even (byte-stable
  `div_round_half_even` copy) or cancel fail-closed; symbol change → relabel;
  dividend → unaffected (DATA-019 scoping). Flat records and OCC option symbols
  are never touched by an equity action. Fallible `PaperCorpActionAlertSink`;
  missed pages surface in `alert_failures`, continue-to-safety.
- **CLI `data021_paper_corp_action_cli`** (atp-simulation bin): `apply` builds
  fixture books through the REAL fill/intake paths; `apply-from-store` is the
  same-source scenario — record flags → store `upsert` → gated fact read →
  `actions_from_facts` → application in event order; an uncovered store REFUSES
  (exit 2). Fail-closed allowlist parser, C0-escaped JSON.
- **Tests**: `srs_data_021_corp_action_facts` (8, atp-data),
  `srs_data_021_paper_corp_action` (17, atp-simulation), 3+1 unit (order book,
  json_escape), `tests/domain/test_data021_paper_corp_action.py` (9, incl. the
  same-source e2e + uncovered-refusal + fail-closed input),
  `tests/property/test_data021_paper_corp_action_property.py` (2 seeded:
  split basis conservation, dividend absolute-P&L invariance).

## Why serialized (passes stays false)

The deterministic core is fully built and scenario-proven over fixtures: paper
virtual positions + average cost adjust for splits/dividends/mergers (clause
1), virtual orders cancel on delisting (clause 2, over the store introduced
for it, placed through the engine's real intake), and the data source is the
SAME SRS-DATA-011 store + coverage gate the backtest reads consume (clause 3,
proven in-process and via `apply-from-store`). What keeps it `passes:false`
(codex r7, the only surviving finding class after 6 real fixes): the
AUTHORITATIVE paper order lifecycle that would make `VirtualOrderBook` the
single store every accepted order flows through does not exist yet — today's
shipped flow fills instantly and rests nothing (SIM-004 pins the always-empty
pending slot), so a runtime caller could route via bare `accept_order` and
bypass the book. That runtime is the SRS-EXE-002 orchestrator (with the
SIM-002 fill-loop evolution that rests limit/stop orders); when it lands it
must hold THIS book as its single order store, wire `apply_and_emit` +
`PaperCorpActionAlertSink`, and the flip is a re-run of the existing scenario
suite over that runtime. → `integrate --mode serialized` + `block --on
SRS-EXE-002` (per the non-convergent-block protocol, with operator
authorization).

## Adversarial rounds

- **r1 BLOCK (codex, 2 high — both real, both fixed):**
  1. *Rename-lineage fail-open*: the fact read retagged split/dividend terms to
     the QUERIED symbol (the price reads' relabeling), but an applier's book
     holds state under the HISTORICAL symbol until the rename fact applies — a
     pre-rename split was silently skipped. Fix: `retag_to: Option<&str>` on the
     lineage event helpers; the fact read passes `None` so every fact carries
     its segment's AS-HELD symbol; price reads unchanged. Regression: the full
     OLD-split-then-rename store-driven journey for a position AND an order.
  2. *Cash mergers unadjusted*: mixed stock-and-cash now remaps with the cash
     leg applied additively on the pre-conversion count (the dividend
     convention, exact + value-conserving; sign-flip → new
     `CashLegCrossesBasis` review); pure-cash (numerator 0, a full disposition)
     and negative cash stay review.

- **r2 BLOCK (codex, 1 high — real, fixed):** `corporate_events_in_window`
  skipped the segment-validity-window guard for structural events, so an
  impossible predecessor delisting/merger (dated after its rename) could
  surface as an applicable fact the book no-ops against. Fix: the collector is
  now fallible and runs `check_action_in_segment_window` for delisting/merger;
  SYMBOL-CHANGE gets a boundary-aware variant (`event_ts == valid_until`
  allowed — the rename record RETIRES its segment; the strict check would
  reject every legitimate rename, a subtlety the reviewer's own recommendation
  missed). Fact AND price reads propagate. Regressions in L5 + a CLI/domain
  refusal e2e (the critic's safety-pairing for coverage.rs).

- **r3 BLOCK (codex, 1 high — addressed):** the order book was standalone (only
  fixture-populated callers reachable). Fix: `VirtualOrderBook::place_accepted`
  routes every placement through the REAL SRS-SIM-001 intake
  (`PaperSimulationEngine::accept_order` — validation + routing), resting each
  accepted leg; the CLI and the new runtime-path scenario test place orders
  through it (a rejected request rests nothing; a composite rests one order per
  leg). The book is CALLER-HELD state fed by the engine's real intake output —
  the exact SIM-003 precedent (`VirtualLedgerBook` is caller-held, fed by real
  fill output, and closed green). Evolving SRS-SIM-002's fill path to trigger
  fills FROM resting orders and persisting the book (SIM-004's reserved slot)
  are those owners' evolutions, not SRS-DATA-021 AC contexts.

- **r4 BLOCK (codex, 2 high):** (1) *retired-predecessor facts* (real, fixed):
  `resolve_lineage` never bounded the QUERIED symbol's segment by its own
  outgoing rename, so querying the held predecessor (OLD) after OLD→NEW could
  surface stale post-retirement OLD actions as valid facts. Fixed: the head
  segment's `valid_until` = its outgoing rename within the cutoff (two renames
  → fail closed; post-cutoff rename doesn't bound, as-of semantics); L5 + CLI
  regressions incl. the legitimate held-predecessor journey. (2) *intake
  bypass* (deferred-runtime class): plain `accept_order` callers don't rest
  orders — TRUE and by design: no accepted order ever rests in the shipped
  SIM-002 flow (SIM-004 pins the always-empty pending slot), so no production
  bypass path exists; structural no-bypass = rewriting SIM-001's green
  compile-time contract from an adjacent feature. Scoped in module docs with
  named owners (SRS-SIM-002 fill-loop evolution / SRS-EXE-002 must adopt the
  book).

- **r5 BLOCK (codex, 1 high — real, fixed):** the fact read stopped at an
  in-window rename — the applier remaps the book onto the successor but never
  saw the successor's LATER in-window actions. Fix: `extend_lineage_forward`
  walks outgoing renames forward (fact read only; price reads untouched), each
  successor getting a validity segment so all existing checks govern the
  forward leg; ONE query per held instrument (doc'd — a second query for
  another name of the same lineage would double-apply). L5 + CLI journey
  regressions.

- **r6 BLOCK (codex, 1 high — real, fixed):** same-instant ordering — a
  successor action at exactly the rename ts sorted before the rename (stable
  sort + insertion order) → applier no-ops it. Fix: secondary sort key
  `same_instant_precedence` (SymbolChange 0 → Split 1 → Dividend 2 → Merger 3
  → Delisting 4); unambiguous because same-instant predecessor actions and
  chained renames already fail closed. L5 + applied-journey + CLI regressions.

## Gotchas for future sessions

- `paper_order.rs` and `coverage.rs` are SAFETY_PATH_RE paths — commits touching
  them need the paired `tests/domain/*.py` diff.
- Ruff "Organize imports" fails (2 pre-existing errors:
  `tests/domain/test_strategy_container_least_privilege.py`,
  `tools/architecture_check.py`) on main too — NOT introduced here; left for a
  dedicated fix.
- `runtime_services.json`'s `virtual_ledger_contract.deferred[4]` /
  `sim_persistence_contract.deferred[4]` correctly attribute corp-action
  ownership to SRS-DATA-021 (they describe what THOSE slices defer) — still
  accurate now that the owner exists; deliberately untouched.
- Sequencing multiple actions is the caller's job — apply facts in the
  `effective_ts` order `actions_from_facts` preserves (the CLI and tests do).
