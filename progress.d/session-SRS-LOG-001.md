=== SESSION SRS-LOG-001 ===
Date: 2026-08-04 (session 2)
Feature: SRS-LOG-001 — separate persistent system logs from user strategy logs.
Outcome: serialized (passes:false) — the OPERATOR SURFACE over the log store is now built and
  integrated (REST `GET /api/v1/logs`, `admin logs` CLI, `LOGS` WebSocket publisher, dashboard log
  pane). What remains is NOT surface: it is the EVENTS those surfaces would carry — five of the
  eight AC-named SYS-61 system sources have no producer anywhere in the tree — plus the
  browser-automation evidence for the dashboard-viewing clause.

Session 1 (2026-06-30, folded below in the prior note's place) built the storage substrate
(`atp_logging/persistence.py`: separated durable JSONL stores, fsync, torn-tail tolerance,
fail-closed corruption, `read_records`/`query`) and deferred the rest to SRS-UI-001 + SRS-API-001.

Why that deferral was stale, and why this was actionable:
- Both of those substrates have since landed, and the runtime contract now assigns the LOGS
  operations back to THIS feature. `python/atp_runtime/contract.py`: "The LOGS feature … is owned
  end-to-end by SRS-LOG-001, the actionable blocker … The runtime only provides the registry/
  dispatch substrate it plugs into."
- So `GET /api/v1/logs`, `admin logs`, and the `LOGS` channel were all still answering the
  structured 501 naming SRS-LOG-001, and the dashboard had no log pane. Nothing blocked building
  them.
- Claimed by operator-select (`tools/work_on.sh`); the branch was already level with origin/main
  (0 ahead / 0 behind), so no stale-worktree refresh was needed.

What I did:
- NEW `python/atp_logs_service/` — the composer package. `atp_logging` may not import any interface
  package (`tools/log_record_check.py` enforces it AST-level) and `atp_runtime` re-exports all three,
  so the handlers CANNOT live in the logging SDK. Same relationship `atp_logging_boot` has to
  credential redaction and `atp_safety`/`atp_readiness` have to their handlers.
  - `handlers.py` — `LogsQueryHandler`, ONE transport-free handler behind both the REST route and
    the CLI command (the SDK declares the same query dimensions on both; a second implementation
    would only be a second place to drift).
  - `publisher.py` — `LogEventPublisher`, its own daemon ticker. The `LOGS` channel declares
    `refresh_seconds=0`, which `atp_dashboard.publisher.cadence_for` deliberately rejects, so it
    must NOT ride DashboardPublisher; publishing a status snapshot on a cadence would drift the
    declared per-event payload.
  - `wiring.py` — `wire_logs(runtime, *, system_store_path, strategy_store_path, ...)`, collaborators
    REQUIRED keyword-only with no fixture fallback (the `wire_kill_switch`/`wire_readiness`
    discipline). Returns an UN-started publisher.
  - `__main__.py` — `python -m atp_logs_service admin logs [...]`, the composed CLI entrypoint.
    `python -m atp_runtime` builds a BARE runtime, so `admin logs` there returns the same honest 501
    that `kill-switch activate` / `readiness wait` do; this is the process that composes it.
- NEW `python/atp_dashboard/logs.py` — `LogPaneProvider` → `GET /dashboard/api/logs`, returning the
  two classes as two separate cells read from two separate files (the payload shape carries the
  separation), plus a per-source producer-coverage map.
- `atp_dashboard/server.py` — `mount_dashboard(..., logs=...)` + `_mount_logs_arm()`, both opt-in on
  ONE env knob `ATP_LOG_DIR`. Unset → no route, no handler, no channel claim.
- SPA: a `data-panel="logs"` pane (two tables, severity filter, coverage strip), its render/poll
  logic in app.js, and pane styles. `LOGS` added to the WebSocket SUBSCRIBE list.
- `architecture/runtime_services.json` — new `log_persistence_contract.operator_surface` block;
  `deferred[]` rewritten from {SRS-UI-001, SRS-API-001} (both now BUILT) to what actually blocks the
  flip: `SRS-LOG-001-core-forwarding`, `SRS-LOG-001-producer-coverage`, `SRS-LOG-001-dashboard-e2e`.
  Reconciled the stale "deferred" prose in `log_record_contract` and the LOGS entry in
  `operator_workflow_surface_contract` (kept named there — `validate_owners` requires every non-
  runtime owner to appear in `deferred[]`).
- `tools/log_persistence_check.py` — 5 new collectors (22 total): operator-surface modules exist;
  the handler matches the SDK-declared operations/params/response fields; an unwired runtime still
  defers to SRS-LOG-001; a corrupt store fails closed on BOTH the handler and the pane; producer
  coverage is stated for every system source. No new check script → nothing to mirror into ci.yml /
  run_ci_locally.sh (this one was already wired).

Key design decisions (the honesty surface):
1. `log_class` selects exactly ONE store on REST/CLI — never a merged read. A `source` belonging to
   the other class is REFUSED (400), not answered with `[]`: `?log_class=strategy&source=kill_switch`
   asked for something that cannot exist, and `[]` would read as "no kill-switch activations".
2. Unreadable is never empty. `LogStoreCorruptionError` → structured 500 (REST/CLI) and
   `records: null` (pane). `{"events": []}` for a trail that could not be read is indistinguishable
   from "nothing happened". `store_present` additionally separates an ABSENT trail from a
   present-but-empty one.
3. Undeclared parameters are refused (400), not silently dropped — accepting and ignoring `?limit=10`
   would report a server-capped page as if the caller's bound had been honoured.
4. Reads are bounded (`max_events`, newest-first) and report `returned`/`matched`/`truncated`.
5. `--follow` is REFUSED with a 400 naming the `LOGS` WebSocket channel as the real streaming
   surface. The `Handler` protocol returns one result and cannot stream to stdout; a batch collected
   over a window is not "events as they arrive". (Operator decision at plan time.)
6. The publisher claims the `LOGS` channel in `start()`, never at wiring time — the runtime counts a
   claimed channel toward workflow readiness, so claiming it while nothing publishes would overstate
   it. Verified: `fully_served` is False after wiring, True only after `start()`.
7. The publisher publishes only records read back from a store; a read failure, or an eviction that
   costs it its place in the trail, is surfaced on `health()` (sticky ok=False) rather than swallowed
   into silence. Going quiet is the dangerous failure for a log channel.
8. Producer coverage is STATED, not implied: `SOURCE_COVERAGE` gives each of the eight SYS-61 sources
   a produced/partial/deferred verdict and names the owner of every gap. Without it, a pane showing
   three kinds of event reads as "the system emitted three kinds of event".
9. ISO-8601 `start_time`/`end_time` resolve to microseconds at best while records are stamped in
   nanoseconds — two records inside the same microsecond cannot be separated by any window this
   declared surface can express. Documented on the parser rather than left as a surprise.

What I tested (per AC step):
- Step 1 (./init.sh → Environment ready): PASS — `./init.sh` → "✓ Environment ready" (it runs the
  extended log_persistence_check inline).
- Step 2 (exercise the surface): PASS for every leg that runs solo —
  * `dispatch_rest("GET","/api/v1/logs")` on a BARE runtime → 501, detail.owner=SRS-LOG-001;
    after `wire_logs` → 200 with the declared events.
  * `?log_class=strategy` → only strategy records; `?severity=WARNING` → 400 LOGS_BAD_SEVERITY;
    `?limit=10` → 400 LOGS_UNKNOWN_PARAMETER; `?log_class=strategy&source=kill_switch` → 400
    LOGS_SOURCE_CLASS_MISMATCH.
  * `ATP_LOG_DIR=<dir> python -m atp_logs_service admin logs --log-class strategy --json` → exit 0,
    strategy records only. `--follow` → exit 2 naming the LOGS WebSocket channel. Knob unset → exit
    USAGE_ERROR (refuses to guess an audit-trail location).
  * `GET /dashboard/api/logs` → both classes as separate cells (system.jsonl / strategy.jsonl) plus
    the 8-source coverage strip. Bare mount → 404 (never an empty trail).
  * Publisher: 3 records → 3 LOGS events, second poll → 0 (no re-broadcast). LOGS workflow
    implemented_operations 2/3 before `start()`, 3/3 + fully_served after.
  * BROWSER-AUTOMATION LEG: NOT RUN — `tests/e2e/test_dashboard_logs.py` is written (both classes
    render from separate stores; a published record arrives as a `LOGS` EVENT frame with the
    SUBSCRIBE frame asserted; a corrupt store renders an explicit error) but binds the dashboard, so
    it is gated behind ATP_RUN_E2E=1 and must not run while siblings hold the shared ports.
- Step 3 (acceptance criteria): PARTIAL, and this is why passes stays false.
  * "stored with timestamp, severity, source, event type, message, correlation ID" → MET.
  * "both log classes are viewable from the dashboard" → MET in code + boundary tests; the
    browser-automation EVIDENCE is owed.
  * "system logs include order routing outcomes, ingestion job lifecycle, container lifecycle, IB
    Gateway connection state changes, kill-switch activations, Hot-Swap events, resource threshold
    alerts, and market data subscription changes" → NOT MET. Verified by grepping `LogRecord(` +
    `Source.X` across python/: only `atp_safety/audit.py` (kill_switch) and
    `atp_dashboard/heartbeat.py` (ib_gateway + market_data heartbeat transitions) build records.
    kill_switch=produced; ib_gateway/market_data=partial (CONNECT/DISCONNECT/RECONNECT and
    SUBSCRIPTION_CHANGE have no producer); order_routing, ingestion, container_lifecycle, hot_swap,
    resource_monitor = NO producer at all.
- Step 4 (record evidence, leave passes false): DONE.
- Commands (all green): tools/log_persistence_check.py PASS (22 collectors); tools/log_record_check.py
  PASS; tools/operator_workflow_surface_check.py PASS; tools/operator_interface_runtime_check.py PASS;
  tools/run_ci_locally.sh exit 0; pytest -m "not integration and not e2e" → 4604 passed, 9 skipped;
  ruff check + ruff format clean on every file I touched; mypy clean on atp_logs_service (5 files).

Critic verdicts:
  deterministic (tools/critic_check.py --staged): APPROVE — 0 findings (safety-paired by
    tests/domain/test_log_operator_surface.py; atp_logging / atp_logs_service / log_* are
    SAFETY_PATH_RE paths).
  judgment (tools/adversarial_review.py origin/main, reviewer=codex):
    r1 → BLOCK [high] "Dashboard never subscribes to LOGS channel": the SPA had an `onEvent` LOGS
      branch and the pane comment claimed live delivery, but `LOGS` was missing from the SUBSCRIBE
      frame — so the live path was dead and the 4s REST poll silently covered for it (the e2e would
      have passed for the wrong reason, waiting 10s while the poll ran every 4s). FIXED: added LOGS
      to the SUBSCRIBE list; corrected the comment; tightened the e2e to assert the SUBSCRIBE frame
      and the received `LOGS` EVENT frame rather than only the rendered text; and added a GENERAL
      static guard (tests/boundary/test_dashboard_logs_wiring.py::
      test_every_handled_channel_is_actually_subscribed) that fails whenever any channel handled in
      `onEvent` is not in the SUBSCRIBE list — it runs in the parallel suite, so this class of defect
      can no longer hide behind an e2e that cannot run here.
    r2 → BLOCK, two [high]:
      (a) "LOGS publisher can die after claiming workflow readiness": `_drain` called `self._publish`
        unguarded, so a wedged fan-out path would kill the daemon thread AFTER `start()` had claimed
        the channel — the runtime would keep reporting LOGS fully_served while nothing published.
        FIXED: the publish call is guarded; a failure records `publish_failures` + `last_error`,
        does NOT advance the cursor past the record that failed (so it is retried, never skipped),
        and breaks that drain instead of the thread. `_run` additionally wraps `poll_once` as a last
        line of defence. `health()` gained `publish_failures` and its `ok` is sticky-false; `running`
        is reported separately so liveness is asked directly rather than inferred from no-errors.
        Regressions: `test_a_failing_fanout_retries_the_record_and_never_kills_the_ticker` and
        `test_the_ticker_survives_an_unexpected_poll_error`.
      (b) "LOGS AsyncAPI contract omits required log_class discriminator": the published event
        carried `log_class` (the SPA routes frames by it) but `atp_ws.channels` declared only the six
        SYS-61 fields — public contract drift, and a generated client would have no documented way to
        keep the two classes apart on the one shared channel. FIXED: added `log_class` to the LOGS
        `payload_fields` with the rationale inline, regenerated the frozen snapshot via
        `tools/websocket_api_check.py --update` (3-line diff), and added
        `test_published_event_matches_the_declared_logs_channel_payload`, which compares
        `atp_logs_service.EVENT_FIELDS` to the declared channel payload so the two cannot drift again.
    r3 → BLOCK [high] "Count cursor skips retained records after rotation": the publisher used a
      per-class COUNT as its cursor and only treated rotation as a gap when the retained list got
      SHORTER. But rotation drops the oldest segments while new records append, so the list can stay
      the same length (or grow) while its contents shift left underneath the index — evict 30 while
      30 arrive and `records[N:]` names the wrong window, skipping real audit events on a poll that
      reports itself perfectly healthy. FIXED by replacing the count with an ANCHOR cursor: the last
      record actually published plus the index it sat at, re-located each poll.
        * anchor still at its index → publish everything after it (the ordinary path);
        * anchor found elsewhere → rotation shifted the trail; publish everything after its new
          position — nothing lost, no gap;
        * anchor GONE → it was evicted. Rotation drops a PREFIX, so everything still retained was
          written AFTER the anchor and has never been published: publish all of it (re-syncing to
          the end would skip exactly the records the operator can still see in the store). The
          unrecoverable window between the anchor and the retained head is counted as an eviction
          gap and surfaced on health().
      Regressions: `test_rotation_that_evicts_while_records_arrive_does_not_skip_them` (drives real
      rotation and asserts every RETAINED post-poll record was published, with no re-broadcast) and
      `test_an_evicted_anchor_publishes_the_retained_history_and_reports_the_gap`.
    r4 → BLOCK [high] "Log query cap is applied after an unbounded full-store read": the handler
      called `read_records(...)` (which materialises every segment) and only THEN sliced
      `max_events`, so a large audit trail could stall or OOM the operator runtime — and the store
      defaults to unbounded append with no eviction, so "large" is the expected end state, not an
      edge case. The pane and the publisher had the same shape.
      FIXED at the substrate, so all three read paths share one bounded implementation:
        * NEW `atp_logging.persistence.iter_records` — a generator streaming rotated segments
          oldest-first then the active segment, one record at a time, preserving the torn-tail and
          fail-closed-corruption rules line by line.
        * NEW `atp_logging.persistence.read_records_bounded(path, *, limit, ...)` — streams matches
          through a `deque(maxlen=limit)` and returns `(newest_first_page, exact_total)`. Memory is
          O(limit); the TOTAL stays exact because counting is free, so the surfaces can still say
          "100 of 40,000" honestly.
        * The filter predicate was extracted (`_filter_predicate`) so `query` (in-memory) and
          `read_records_bounded` (streaming) cannot answer differently.
        * `LogsQueryHandler` and `LogPaneProvider` now call the bounded reader; the publisher
          streams via `iter_records` and buffers at most `max_events_per_poll` records
          (`_collect_after_anchor`), which also simplified the anchor cursor — the buffer resets
          each time the anchor is seen, so the last occurrence wins without any index bookkeeping.
      HONEST LIMIT (deliberate, documented on `iter_records`): I/O is still O(trail) — every line is
      read. That is what lets corruption ANYWHERE in the trail fail the read closed; a tail-only
      scan would bound the I/O but would silently stop validating older history, which is a worse
      trade for an audit log. A byte-offset index would give O(page) I/O and is a separate concern.
      Contract: `required_exports` + `read_functions` updated (+ `bounded_read_function` /
      `streaming_read_function`), and a new check collector `check_reads_are_memory_bounded` asserts
      the streaming function is a generator, the page is capped/newest-first with an exact total, and
      the handler actually uses the bounded reader. Regressions:
      `test_query_never_materialises_the_whole_trail` and
      `test_publisher_buffers_at_most_one_page_per_poll`.
    r5 → BLOCK [high] "Anchor cursor treats duplicate log records as already published": the anchor
      was compared by `LogRecord` VALUE equality, so a second byte-identical line — a retried
      operation writing the same message with the same correlation id in the same nanosecond, which
      is a perfectly valid audit record — was mistaken for the anchor, cleared the buffer, and was
      never published. FIXED by keying the cursor on PHYSICAL POSITION instead:
        * NEW `atp_logging.persistence.RecordPosition` (segment st_dev + st_ino + end byte offset)
          and `iter_records_with_positions`, which pairs every streamed record with the identity of
          the line that produced it. device/inode identifies the segment FILE rather than its name,
          so a rotation (rename + fresh active file) is seen as the same bytes under a new name.
        * `_collect_after_anchor` now compares positions; `iter_records` is a thin wrapper that
          drops them. Identical content no longer collides because positions are unique.
      Regression: `test_byte_identical_records_are_each_published_exactly_once` (verified to FAIL
      under the old value cursor: both duplicates matched the anchor, so it published 0). New check
      collector `check_resume_cursor_is_physical_not_value_based` asserts identical records get
      distinct positions AND that the publisher keys on the position type.
    r6 → BLOCK, two [high] + one [medium] — all contract-vs-implementation drift I had introduced
      while claiming the opposite ("the response shape is the frozen contract's, verbatim"):
      (a) "REST logs response drifts from OpenAPI": the live body carries `log_class` per event plus
        the `returned`/`matched`/`truncated`/`limit`/`store_present`/`event_fields`/`srs_ref`
        envelope, none of which the declared `response_fields` listed. FIXED by declaring them (the
        same direction as the r2 WS fix — the metadata is load-bearing honesty, so the contract
        moves to meet it, not the other way) and regenerating `openapi.json` (+24 lines). The check
        collector now serves a REAL request and compares the emitted surface to the declaration in
        BOTH directions, and `test_response_shape_matches_the_declared_contract_exactly` does the
        same at L1 — a superset is undeclared drift, a subset is an unkept promise.
      (b) "CLI contract advertises streaming that the handler refuses": `--follow` was declared and
        always returned LOGS_FOLLOW_UNSUPPORTED. The reviewer is right that this is the wrong shape
        for the operator decision taken at plan time — "uncovered capability → no public surface"
        means the SURFACE must not advertise it, and a flag that always errors is still a surface.
        FIXED by removing `--follow` from `atp_cli.commands` entirely and regenerating `manual.json`;
        the command summary now says "streaming: subscribe to the LOGS WebSocket channel". argparse
        rejects the flag before dispatch, and the handler's unknown-parameter guard still refuses an
        injected one (defence in depth).
      (c) "Time filter can exclude exact boundary records" [medium]: `_parse_time_ns` scaled
        `datetime.timestamp()` (a float, ~240 ns of resolution at current epoch values) to
        nanoseconds, so an inclusive `start_time` could land ABOVE the record it named. Verified
        empirically: 2023-11-14T22:13:20.007919Z → ...007919104 ns vs the true ...007919000, and a
        query for that exact instant returned 0 records under float math and 1 under integer math.
        FIXED with exact integer arithmetic from a UTC epoch delta. Regression:
        `test_boundary_holds_at_a_stamp_float_math_would_miss` (round-second fixtures never expose
        this, so the case is explicit) plus inclusive-boundary assertions on every fixture record.
    r7 → BLOCK, two [high]:
      (a) "Missing log stores render as healthy empty logs": I had added `store_present` to the
        payload but still returned `ok:True` + `records:[]` when the configured file was absent.
        Reading a missing file SUCCEEDS and yields zero records — the same shape as a healthy quiet
        system — so a deleted store, a mispointed ATP_LOG_DIR, or a writer that never started would
        show a green pane over missing audit history. FIXED: both surfaces now fail closed on a
        missing store, with its OWN error type (`LOGS_STORE_MISSING`) so it is never confused with
        corruption (present-but-unreadable): REST raises 500, the pane returns `ok:False` +
        `records:None`, and the SPA renders "log store MISSING" distinctly from "unavailable".
        Boundary regression `test_configured_but_missing_stores_fail_closed` drives it through
        `ATP_LOG_DIR` pointed at an empty directory; new check collector
        `check_missing_store_fails_closed`.
      (b) "OpenAPI schema contradicts the live logs response": every response field was documented
        as `string` (the placeholder default for a route with no handler), while the live handler
        returns an array of events, integer counters, and booleans. FIXED by populating the
        route's existing `field_types` seam — which exists precisely for this ("a placeholder that
        contradicts a live handler is worse than no schema") — and regenerating `openapi.json`.
        New contract test `test_openapi_documents_the_real_response_types` serves a real request and
        compares each documented type against the Python type actually returned.
    r8 → BLOCK [high] "Logs response schema exposes nested event fields as top-level properties":
      a direct consequence of the r6 fix — `Route.response_fields` is FLAT, so declaring the
      per-event fields there documented `timestamp`/`severity`/... BESIDE the `events` array instead
      of inside it. A generated client would look in the wrong place, which is a wrong public
      contract rather than a vague one. (My own r7 type test missed it: it iterated the emitted body
      and looked each key up in the schema, so a documented-but-absent field was invisible.)
      FIXED by adding nested support to the declarative surface: new
      `Route.response_item_fields` (array field → its item fields+types), rendered by
      `atp_api.openapi` as `events.items.properties` with `additionalProperties:false`; the
      per-event fields moved OUT of the flat `response_fields`. `openapi.json` regenerated.
      Both the L1 test and the check collector now compare the two LEVELS separately — top-level
      set equality plus item-level set equality — and the contract test additionally asserts no
      per-event field appears as a top-level property.
    r9 → BLOCK [high] "LOGS publisher can skip retained records if rotation happens mid-scan":
      `iter_records_with_positions` enumerates segment PATHS and then opens them, so a rotation
      landing in that window makes the scan incoherent — the old active segment can be renamed past
      a path already visited, and its post-anchor records never appear in that scan. Acting on it
      would drop them (and could re-publish an older span). FIXED with a rotation-stability guard:
      the active segment's (device, inode) is taken before and after each scan, and if it moved the
      read is DISCARDED — nothing published, cursor untouched — and retried (up to
      `_ROTATION_RETRIES`). Discarding is always safe here because the trail is append-only, so a
      later scan sees a superset. Exhausting the retries publishes nothing and records
      `rotation_races` + `last_error`.
      Deliberate call on `health()["ok"]`: it stays TRUE for a rotation race. `ok` means "no event
      may have been MISSED"; a raced scan is a DELAY (the records are on disk and the next poll
      carries them), and folding a delay into a data-loss flag would blunt the flag's meaning. The
      condition is surfaced through the counter and `last_error` instead. Documented on `health()`.
      Regressions: `test_rotation_during_a_scan_loses_no_records` (rotation forced mid-scan; the
      retry recovers and the pending record still goes out exactly once) and
      `test_a_store_rotating_through_every_attempt_publishes_nothing_and_says_so` (every attempt
      raced → 0 published, counter set, and a later clean poll still delivers it exactly once).
    r10 → BLOCK [high] "LOGS publisher treats missing audit stores as a healthy empty stream": the
      r7 missing-store rule had been applied to the REST handler and the pane but NOT to the
      publisher — my own consistency gap. A missing path yields an empty scan, the identity check
      trivially agrees, and nothing is recorded, so the channel published silence while reporting
      `ok:True`; worse, `start()` claimed the channel unconditionally, so the runtime reported the
      LOGS workflow `fully_served` over a trail nobody could read. (The reviewer verified this
      locally.) FIXED:
        * `_drain` records a read failure for a configured-but-absent store (sticky `ok:False`),
          matching the handler's LOGS_STORE_MISSING and the pane's `records:None`;
        * the channel claim MOVED off `start()` — it now happens on the first poll that reads BOTH
          trails cleanly, so readiness cannot report LOGS served over a missing trail, and a trail
          whose writer boots after the dashboard is still picked up automatically;
        * `start()` runs that first poll SYNCHRONOUSLY (through the same guard as the ticker) so the
          claim is deterministic rather than racing the thread.
      Regressions: `test_missing_stores_are_a_read_failure_not_a_healthy_empty_stream`,
      `test_the_channel_is_not_claimed_while_a_store_is_missing` (claims 0 while absent, exactly 1
      once the writer creates the trail, never twice), and boundary
      `test_logs_workflow_is_not_served_over_a_missing_audit_trail`.
    r11 → BLOCK [high] "Public contract promises structured --follow handling that code removed":
      stale prose left by my own r6 fix — `operator_surface` still carried `follow_rejected: true`
      and described a structured 400, while the CLI no longer declares the flag at all (argparse
      rejects it first). Drift in the OPPOSITE direction from the usual one, and just as
      misleading: a consumer would look for a machine-readable error and get a usage failure.
      FIXED: `follow_rejected: true` → `follow_declared: false`, and the notes now state that the
      capability is not advertised anywhere (an uncovered capability gets no public surface, and a
      flag that always fails is still a surface), that the command summary points at the LOGS
      WebSocket channel, and that the handler's unknown-parameter guard still refuses an injected
      `follow` for a hand-built Request. New collector `check_follow_is_not_advertised_anywhere`
      cross-checks all four places at once — contract, `atp_cli.commands`, the frozen manual, and
      the handler's CLI_PARAMS — so this cannot drift again in either direction.
    r12 → BLOCK, one [high] + one [medium]:
      (a) "Strategy log identity is stripped from every operator surface": `render_event` — the ONE
        renderer behind REST, the LOGS channel, and the pane — omitted `strategy_id`, even though
        the record schema REQUIRES it on strategy-class records. Since `source` is always the
        literal `strategy` on those records, every one of the 30 Reservoir strategies produced
        indistinguishable lines and no operator could tell which strategy emitted what. A real
        functional loss on the audit surface, not a cosmetic one. FIXED end to end: `strategy_id`
        added to `EVENT_FIELDS` + `render_event`, the OpenAPI `events[].items` schema (as the
        `string|null` union — null on system records, where the schema forbids an id), the LOGS
        AsyncAPI payload, the contract's `event_fields`, and the pane (rendered inline in the source
        cell as `strategy · <id>`). The nested schema builder gained the same union rendering the
        top level already had. Regression
        `test_strategy_lines_stay_attributable_on_every_surface` writes two DIFFERENT strategies and
        asserts attribution survives REST, CLI, the pane, AND the WebSocket channel — plus that a
        system record reports `strategy_id: null` rather than inventing one.
      (b) "CLI advertises strategy-id filtering that the handler rejects" [medium]: `--source` was
        documented as "Source component or strategy id", but the value is validated against the
        SYS-61 `Source` enum, so a real id like `sma-1` came back LOGS_BAD_SOURCE. FIXED by
        correcting the declared summary to what the option actually accepts (adding a strategy_id
        FILTER would mean extending the frozen REST request-field set, which the AC does not ask
        for). Strategy lines are reached with `--log-class strategy`, and every rendered event now
        carries `strategy_id` for attribution — which is what fix (a) makes possible.
    r13 → BLOCK [high] "LOGS WebSocket events can be clobbered by stale REST poll snapshots": the
      pane has TWO feeds for one buffer, and they race — a `/dashboard/api/logs` poll that STARTED
      before a live event resolves AFTER it, and the render replaced the class buffer wholesale, so
      the event vanished from the pane until some later poll happened to include it. An audit event
      that appears and then disappears is worse than one that arrives late. FIXED by merging instead
      of replacing: live events are held in `logLiveEvents[cls]` (capped, oldest dropped first) and
      overlaid on every snapshot until the snapshot itself carries them, keyed on the full rendered
      identity so nothing is double-rendered; a class that goes unreadable drops its overlay too.
      TESTING NOTE — this is JS, so a Python assertion could only ever check the source text. Node
      is available, so `tests/boundary/test_dashboard_logs_interleaving.py` runs the REAL app.js
      render path under node with a small DOM stub, drives the exact interleaving, and asserts the
      event survives (and is not duplicated once the snapshot does carry it). Verified as a genuine
      regression: the same harness against a copy of app.js with the merge reverted returns
      `["already persisted"]` — the live event erased — versus
      `["arrived over the channel","already persisted"]` with the fix. It skips (never silently
      passes) where node is absent, and the gated e2e additionally asserts survival across a real
      poll in the browser.
    r14 → BLOCK, one [critical] + one [high], both publisher lifecycle:
      (a) [critical] "Stop timeout can create two active LOGS publisher threads": `stop()` joined
        with a budget and then dropped the thread reference UNCONDITIONALLY. A ticker wedged in a
        read or a publish outlives that budget, so the next `start()` would clear the stop flag and
        launch a SECOND ticker beside the first — two loops sharing one anchor cursor, duplicating
        and skipping audit events. FIXED: a timed-out join KEEPS the reference, counts
        `stop_timeouts`, and explains itself on `last_error`; `start()` refuses while the old thread
        is alive, and reclaims the slot only once it has genuinely exited (so a thread that finishes
        after we stopped waiting does not block the publisher forever).
      (b) [high] "Publisher claims readiness after publish failure": the first-claim condition
        looked only at `read_failures`, so a poll that read both trails but whose fan-out RAISED
        still claimed the channel — runtime readiness counting a stream that had delivered nothing.
        FIXED: the claim now requires a poll clean in EVERY dimension (read, publish, rotation,
        eviction), via a `_fault_counts()` snapshot compared across the poll.
      Regressions: `test_a_stop_that_times_out_never_leaves_two_tickers` (wedges the TICKER — not
      start()'s synchronous first poll — then proves stop times out, start refuses, and the slot is
      reusable once it exits) and `test_the_channel_is_not_claimed_when_the_first_fanout_fails`.
    r15 → BLOCK, two [high]:
      (a) "AsyncAPI rejects valid system LOGS events": the r12 `strategy_id` addition landed in the
        WS channel's `payload_fields`, but the AsyncAPI generator had no typed-field support, so it
        was documented as a bare `string` — and a SYSTEM record publishes `null` there (the record
        schema forbids an id), i.e. the COMMONEST event on the channel was schema-invalid. FIXED by
        giving `EventChannel` the same `field_types` seam `Route` already had, teaching the AsyncAPI
        generator the `string|null` union rendering, declaring it on LOGS, and regenerating the
        snapshot.
      (b) "LOGS readiness is latched after first clean poll": once claimed, the channel stayed
        claimed forever, so `/api/v1/system/status` kept reporting the LOGS workflow fully served
        after the publisher had lost a store. FIXED by making the claim REVOCABLE: new
        `OperatorInterfaceRuntime.unregister_publisher` (a claim answers "is this being published?",
        and that can stop being true — readiness that cannot be revoked eventually lies), and the
        publisher releases/re-claims as delivery health changes.
        Deliberate call on WHICH faults move readiness: read failures and publish failures do (the
        channel is not delivering now); rotation races and eviction gaps do NOT. A race is a delay
        and a gap is a fact about lost HISTORY — and the re-claim path proves the point, because a
        restored-but-emptied trail produces an eviction gap, so gating on it would latch readiness
        OFF forever, the same lie in reverse. Both stay visible on `health()`.
        Regression `test_logs_readiness_is_revoked_when_the_trail_is_lost`: claim, delete the store
        mid-flight, poll → not registered / not fully_served; restore the trail, poll → registered
        again.
    r16 → BLOCK [high] "Dashboard log dedupe can erase distinct repeated audit records": the r13
      overlay merge keyed on RENDERED VALUES, and audit values repeat by design — a retried
      operation writes the same message with the same correlation id, and the rendered timestamp is
      only milliseconds. A stale snapshot containing an older identical-looking record would make
      the newer live event look "already persisted" and drop it. Same family as r5 (value is not
      identity), one layer up. FIXED by publishing a real identity: `RecordPosition.token` (an
      OPAQUE `device-inode-offset` string; the format is explicitly not contractual) is rendered as
      `record_id` on every event, so the REST poll, the LOGS channel, and the pane all name the same
      persisted line the same way. `read_records_bounded` now returns `(position, record)` pairs so
      the query surfaces have it too; the SPA merge keys on `record_id` with the value-join kept
      only as a fallback for an id-less payload. Declared in the OpenAPI `events[]` item schema, the
      AsyncAPI payload, and the contract's `event_fields`.
      Regressions: `test_identical_records_get_distinct_record_ids_on_every_surface` (two
      byte-identical records in the same nanosecond get different ids on REST, the pane, and the
      channel — and the SAME id across surfaces, so a consumer can match them) and, in the node
      harness, `test_two_identical_looking_records_are_not_deduped_into_one`.
    r17 → BLOCK [high] "LOGS publisher stop leaves channel claimed": r15 made the claim revocable
      but I did not carry that into `stop()`, so a cleanly stopped publisher left LOGS reported as
      fully served by a ticker that no longer existed — the runtime outlives the publisher (a
      dashboard can stop its log arm and keep serving). FIXED: `stop()` releases the claim BEFORE
      joining and regardless of how the join goes (a timed-out join then errs toward UNDERstating
      readiness, which is the safe direction). Two follow-on ordering bugs surfaced while proving
      it and were fixed with it: a poll already in flight when `stop()` was called would re-claim,
      so the claim path now refuses while a stop is pending; and `start()` polled BEFORE clearing
      the stop flag, so a restart could never re-claim — it clears first now.
      Regression `test_stopping_the_publisher_drops_logs_readiness`: start → served, stop → not
      served (while the REST/CLI handlers stay registered, since they still answer), start → served
      again.
    r18 → BLOCK [high, confidence 0.74, reviewer-flagged as an inference] "Record cursor is reusable
      after segment eviction": rotation UNLINKS the oldest segment, the filesystem may reuse that
      inode, and a later record can land at the same byte offset — so a position-ONLY compare could
      read a brand-new line as the anchor and skip retained history silently.
      FIXED as far as this feature can: the anchor is now the POSITION **and** the RECORD, so a
      false match needs the same physical slot AND identical content at once. A position collision
      carrying different content is correctly treated as an evicted anchor — gap reported, retained
      history published — which is what
      `test_a_reused_position_with_different_content_is_not_the_anchor` drives (anchor's line
      deleted, every entry reporting the anchor's position, assert both retained records still
      publish in order and the gap is surfaced).
      SCOPED, NOT HIDDEN: the residual (inode reuse AND offset collision AND identical content, all
      three at once) cannot be closed by a cursor at all — it needs a NON-REUSABLE identity, i.e. a
      monotonic sequence persisted in the record envelope. That is a change to the on-disk log
      format, which the SRS-DATA-015 schema registry governs (version gate + migration), not this
      feature. Recorded as the `SRS-LOG-001-record-sequence` entry in
      `log_persistence_contract.deferred[]` with that owner named, so the boundary is visible rather
      than papered over.
    r19 → WARN (first non-block), one [medium]: the `--follow` prose I fixed at r11 in
      `log_persistence_contract` was ALSO present in `operator_workflow_surface_contract` — my r11
      collector only scanned this feature's own block, so the duplicate survived. FIXED the second
      sentence, and broadened `check_follow_is_not_advertised_anywhere` to scan EVERY contract block
      for a promised structured rejection, since the per-block scope is exactly what let it hide.
    r20 → BLOCK [high] "Missing log store can still be treated as a clean empty stream": the
      missing-store rule (r7/r10) was an `exists()` PRE-CHECK followed by a scan — a TOCTOU. A
      deletion or rotation landing in that window left the scan yielding nothing, which is exactly
      what a healthy empty trail looks like, so the advertised fail-closed rule was not atomic on
      any of the three surfaces. FIXED at the reader: new `LogStoreMissingError`, and
      `iter_records_with_positions` / `iter_records` / `read_records_bounded` take
      `require_active=True`, which OPENS the active segment and lets absence surface from the open
      itself (no pre-check to race). The handler, pane, and publisher all read that way; the
      `exists()` calls remain only as fast paths with nicer messages, and correctness no longer
      depends on them. A rotated segment that is simply absent stays non-fatal, as it must.
      Regression `test_a_trail_that_vanishes_mid_read_is_reported_on_every_surface` deletes the
      active store INSIDE the iterator (after any pre-check, before the open) and asserts all three
      surfaces report it — REST `LOGS_STORE_MISSING`, pane `records: None`, publisher read-failure
      with no claim — while the intact class keeps publishing (one broken trail must not silence the
      other). This also exposed an unfaithful fixture in the r9 rotation test, which renamed the
      active segment without creating a new one; real rotation does both, and it now does too.
    r21 → BLOCK, one [high] + one [medium]:
      (a) "Log queries can miss records during rotation and still return 200": the r9 rotation-race
        guard lived only in the PUBLISHER, so the REST/CLI query and the dashboard pane could still
        return a page a mid-scan rotation had shortened — a successful-looking but quietly short
        answer, which is the false negative an audit query must never give. FIXED at the substrate
        so every reader inherits it: `read_records_bounded` now takes the active segment's identity
        before and after the scan and retries, raising the new `LogStoreRotationError` after
        `ROTATION_READ_ATTEMPTS` instead of returning a possibly-short page. The identity probe
        moved into `atp_logging.persistence.active_identity` so there is ONE implementation (the
        publisher's streaming loop now uses it too). The handler maps it to a 500
        `LOGS_ROTATION_RACE`; the pane degrades to its explicit unavailable cell.
        Regression `test_a_query_racing_rotation_fails_closed_instead_of_returning_a_short_page`.
      (b) [medium] The package README still described the old `--follow` 400. FIXED to match the
        shipped behaviour (not declared; argparse refuses it before dispatch).
    r22 → BLOCK, one [high] + one [medium]:
      (a) "LOGS readiness can be re-claimed during shutdown": `poll_once` set `_claimed` under one
        lock and then called `_claim_channel()` UNLOCKED, so a `stop()` landing in that window
        released the claim and cleared the flag — and the pending callback then re-registered the
        channel, leaving the runtime reporting LOGS served by a stopped ticker. FIXED with a
        dedicated `_claim_lock` held across BOTH the decision and the callback, in `poll_once` and
        in `stop()`, so the two serialize. (The callbacks are trivial registry set operations that
        never re-enter the publisher, so holding a lock across them is safe.) Regression
        `test_a_stop_interleaved_with_a_claim_leaves_the_channel_released` starts a stop from
        INSIDE the claim callback and asserts exactly one claim + one release, ending released.
        Writing it surfaced the intended consequence worth knowing: `stop()` now BLOCKS while a
        poll is mid-claim, so a claim callback must not itself wait on stop.
      (b) [medium] "`admin logs` declares only OK despite live error exits": a now-LIVE command
        kept the placeholder `exit_codes=(OK,)` while its handler really returns 400 (→
        USAGE_ERROR) and 500 (→ 70) for a missing/corrupt/rotation-racing store — the manual
        promised automation a surface that cannot fail. FIXED by declaring
        `(OK, USAGE_ERROR, INTERNAL_ERROR)` and regenerating the manual. `ExitCode` had no member
        for the 500 path even though `atp_runtime.cli_dispatch` already produced 70 as a bare
        constant, so `ExitCode.INTERNAL_ERROR = 70` was added and the dispatcher now refers to it —
        every live command can now declare that outcome instead of it being undocumented.
    r23 → BLOCK [high] "LOGS readiness ignores rotation-race failures": at r15 I excluded BOTH
      rotation races and eviction gaps from the delivery decision, but they are not alike. A
      rotation race means the poll published NOTHING from that store — non-delivery, now — and it is
      TRANSIENT, so counting it cannot latch readiness off. An eviction gap is a fact about lost
      HISTORY and persists (a restored-but-emptied trail produces one on the very poll that resumes
      delivery), so gating on it WOULD pin readiness off forever. FIXED: rotation races now count as
      non-delivery for claim/release; eviction gaps still do not, with the asymmetry and its reason
      written into the code. Regression
      `test_logs_is_not_served_while_rotation_blocks_every_read`: every read races → LOGS unclaimed
      and not fully_served; rotation stops → the next poll claims (transient, not latched).
    r24 → BLOCK [high] "Dashboard log polling scans the entire audit trail every refresh": at r4 I
      accepted O(trail) I/O as an honest limit for a per-request query — but the pane POLLS it every
      four seconds, forever, against an append-only store, which is a different proposition: the
      operator UI becomes a load that grows without bound. The reviewer was right that this needed
      fixing, not documenting. FIXED with a real tail reader:
        * NEW `atp_logging.persistence.read_tail` — reads segments newest-first, each backwards in
          64 KiB blocks, and STOPS when the page is full, so the cost is proportional to the PAGE.
          It is rotation-stable (same retry as the full read) and honours `require_active`.
        * Verified against the forward reader for exact equivalence — records AND positions — across
          single-block, multi-block (3 KiB lines), torn-tail, and rotated-segment cases, so both
          readers name the same record the same way (`record_id` depends on it).
        * The PANE uses it; the operator's own `GET /api/v1/logs` keeps the full scan. The trade is
          stated rather than hidden: a tail read reports NO exact total (`matched: null`,
          `page_only: true`, and the SPA renders "newest N" instead of inventing a figure) and
          validates only the window it scanned. The exact count and whole-trail corruption detection
          stay on the explicit operator query.
      Regression `test_the_polled_pane_reads_the_page_not_the_trail` writes a 2,000-record trail and
      MEASURES BYTES READ, asserting the pane touches less than half the file — a row-count
      assertion would have passed on a full scan.
    r25 → BLOCK [high] "Tail reader still races missing active store into a clean result": the tail
      reader I added in r24 reintroduced the very TOCTOU r20 had removed from the forward reader —
      `require_active` was a `base.exists()` pre-check, while the backwards open swallowed
      FileNotFoundError, so a deletion in that window could return a rotated-only page as a
      successful read. FIXED the same way as r20: `required` is threaded into
      `_iter_segment_lines_reverse` and `_read_tail_once`, the open itself raises
      `LogStoreMissingError`, and the pre-check is gone. The regression now injects the deletion
      INSIDE `_read_tail_once` (after read_tail's own entry, right before the segment open) so it
      drives the real window rather than a pre-entry one.
      LESSON worth carrying: a new read path does not inherit the guarantees of the old one. Both
      readers now enforce absence at the open, but nothing structural stops a THIRD reader from
      forgetting again — the guard is the shared `require_active` parameter plus the domain test
      that drives all three surfaces.
    r26 → BLOCK [high] "Rotated log writes do not fsync the newly created active segment directory
      entry": a genuine DURABILITY defect in session 1's `JsonlLogStore._rotate_locked` (pre-
      existing on main, but this feature's own crash-durability contract). Rotation renamed the
      active segment, fsynced the directory for the renames, then CREATED the new active file — with
      no directory fsync after the create. fsync on a file makes its contents durable, not its name,
      so a crash could leave the first post-rotation record (a kill-switch ACTIVATION, an IB
      disconnect) written into a file whose directory entry never existed. FIXED with a second
      `_fsync_dir` after the create, inside `_rotate_locked`, so the name is durable before the
      caller's write is considered committed. Regression
      `test_rotation_makes_the_new_segments_directory_entry_durable` records, per directory fsync,
      whether the active segment existed at that moment: pre-fix every marker is False (the only
      fsync preceded the create), post-fix each rotation shows False THEN True. Verified empirically.
    r27 → BLOCK [high] "Publisher readiness registry is mutated cross-thread without a runtime
      lock": my revocable claim (r15) is what made this reachable — before it, `_publishers` only
      ever GREW, so a concurrent reader could only see monotonic progress; now the publisher's
      background thread adds AND removes while status requests read. Individual set operations are
      atomic under the GIL, so nothing corrupts, but `_workflow_status` inspects the set once per
      channel, so ONE report could straddle a transition and describe a moment that never happened.
      FIXED in `atp_runtime`: a `_publisher_lock` guards register/unregister/is_publisher_registered,
      and `_workflow_status` takes a single snapshot of the claims for the whole report.
      Regression `test_status_stays_self_consistent_while_logs_claims_and_releases` flaps the
      registry from one thread while another polls `status_snapshot()`, asserting no report is ever
      internally contradictory (fully_served with deferred owners, or implemented > total) and that
      status never raises.
    r28 → BLOCK [high] "LOGS readiness remains claimed after unexpected poll failure":
      `_poll_guarded`'s catch-all kept the ticker alive (deliberate — a monitoring surface must not
      die) and recorded the failure, but did not release the claim. Surviving is not delivering: a
      loop erroring out on every poll would keep the runtime reporting LOGS served. Every other
      non-delivery path already released; this was the one that slipped. FIXED: the catch-all now
      releases under `_claim_lock`, like `stop()`. Regression
      `test_an_unforeseen_poll_failure_gives_the_logs_claim_back`: start (claims), break
      `poll_once`, and the registry drops LOGS while the ticker stays alive.
    r29 → BLOCK [high] "Architecture contract still defers implemented log surfaces":
      `log_record_contract.description` still told downstream agents the pane / REST / CLI / LOGS
      publisher were somebody else's deferred work. I had fixed that block's deferred[] ENTRIES at
      r6 but not the block's own description — the third time this exact claim has been found in a
      place I had not swept (r11: operator_surface flags; r19: operator_workflow_surface_contract;
      now this). FIXED, and this time with a GUARD rather than another one-off correction: new
      collector `check_no_block_still_defers_a_built_surface` scans EVERY contract block for
      phrasings that describe a shipped log surface as deferred. It immediately caught a THIRD
      instance I had also missed — the opening sentence of the `log_record_contract` SRS-API-001
      deferred entry, whose tail I had rewritten at r6 while leaving its lead-in intact.
      Worth carrying: when the same statement is duplicated across contract blocks, fixing
      instances one at a time does not converge — write the check.
    r30 → BLOCK [high] "logs:store-class-boundary": a wrong-class record physically present in a
      store (legacy data, a hand-edited file, a bad recovery — the store itself refuses to WRITE
      one) was handled inconsistently and wrongly on both paths: the PUBLISHER streamed the file
      unfiltered and fanned the record out, while the query and pane passed a `log_class` FILTER and
      silently dropped it. The reviewer's detail was off (the query does not publish it) but the
      substance is right: neither behaviour is acceptable. Publishing carries a broken separation to
      every subscriber; filtering hides that the invariant this whole feature exists to keep has
      been broken. FIXED by enforcing separation on the READ as well as the write: new
      `LogStoreClassMismatchError`, and every reader takes `expect_class`, validated BEFORE any
      caller filter (filtering first would destroy the evidence). REST → 500
      `LOGS_STORE_CLASS_MISMATCH`, pane → explicit unavailable cell, publisher → read failure with
      no claim. Regression `test_a_contaminated_store_fails_closed_on_every_surface` writes a
      strategy record into `system.jsonl` by hand and asserts all three refuse — and that the
      uncontaminated strategy trail keeps publishing.
    r31 → WARN, one [medium] "LOGS publisher is started before startup can be rolled back":
      `serve()` starts the publishers and only then binds, so a bind failure (port in use, refused
      host) would leave tickers polling the audit stores and publishing into a runtime that never
      came up — invisible work behind a process that looks dead. Half pre-existing (the dashboard
      publisher had the same shape) but my log arm adds a second ticker to leak, so I fixed it
      rather than taking the warn override: startup is now all-or-nothing, stopping whatever
      started before re-raising. Regression `test_a_failed_bind_leaves_no_publisher_running` forces
      `runtime.start()` to raise and asserts no publisher is left running.
    r32 → BLOCK [high] "Dashboard hides older log-store corruption": the r24 tail read validates
      only the window it scans — which I documented — but the pane still reported `ok: true`, and
      that READS as "this trail is healthy". A corrupt line further back is neither detected nor
      denied by a page read, so the green pane was overclaiming. FIXED as a reporting change (not a
      re-scan, which would reinstate the O(history) poll r24 removed): each class cell now carries
      `integrity_scope: "page"`, the contract defines `ok` as "the read succeeded" rather than a
      health verdict, and the SPA renders "newest N · older history not verified here" beside the
      count so an operator is never shown a verified-looking trail that was not verified. The
      verification they CAN rely on is `GET /api/v1/logs`, which scans the whole trail and fails
      closed. Regression `test_the_pane_never_claims_health_it_did_not_verify`: a corrupt line
      followed by well over a page of good records — the pane renders its page and declares the
      scope, and the full query still raises `LOGS_STORE_CORRUPT`.
    r33 → BLOCK [high] "Public API docs still mark live LOGS as contract-only": I made the surface
      answer but left the three FROZEN public documents saying it does not. Every unimplemented
      entry carries "Contract only. Concrete … land with the downstream feature that owns the
      handler"; that sentence is right until a handler lands and exactly backwards afterwards — it
      tells an integrator not to build against an endpoint that returns audit records, and tells an
      operator the trail is unavailable while `admin logs` works. Same drift CLASS as a stale
      `field_types`, which this repo already fixed declaratively, so the fix follows that
      precedent rather than special-casing prose: `Route.served_by` / `Command.served_by` /
      `EventChannel.served_by`, set to `SRS-LOG-001` on the three LOGS entries, swap the
      placeholder for a sentence naming the implementer — while STILL stating that a deployment
      which has not composed the handler returns 501 / exits non-zero / registers no publisher, so
      "served" is never read as "served unconditionally". Regenerated all three snapshots via
      their own `--update` tools: exactly ONE line changed in each, no collateral. Guarded by
      `check_public_docs_do_not_call_a_live_surface_contract_only` (asserts the declaration AND
      the generated artefact, so a regeneration cannot silently restore the placeholder) and
      `test_the_public_contracts_do_not_call_the_live_logs_surface_a_placeholder`, which also
      pins a still-unserved channel to the placeholder so the test cannot pass by making every
      description implemented-shaped.
      SCOPE, stated rather than silently left: the same staleness affects the hot-swap routes
      (SRS-RESV-003), the kill-switch REST/CLI operations (SRS-SAFE-001), and `readiness wait`
      (SRS-API-001) — all have live handlers and all still read "Contract only". Those are other
      features' public contracts and their frozen evidence; sweeping them here would re-freeze
      three snapshots for features I am not working on. The mechanism is now in place for them,
      and this is recorded in `log_persistence_contract.operator_surface.public_docs_note`.
    r34 → BLOCK [high] "Class-bound store read does not enforce its own log class": I had added
      `expect_class` to the streaming readers (r-series) and to the pane, but `JsonlLogStore.read()`
      — the method on the object that CLAIMS a class — still read every segment and handed the lot
      to `query()`. `write()` guards the way in; a trail restored from backup, recovered onto the
      wrong path, or hand-edited can still hold a foreign record. The reviewer's framing is the
      part worth keeping: the same contamination then has two DIFFERENT wrong outcomes depending
      on how the caller happens to filter — unfiltered it comes back as though it belonged to this
      trail, filtered on anything else it is silently dropped and the broken separation leaves no
      trace at all. FIXED at the boundary that owns `_log_class`: the check runs per record BEFORE
      `query()`, so no filter can launder it. Regression
      `test_a_contaminated_store_fails_closed_on_its_own_read` asserts BOTH reads raise, and
      `check_separation_is_enforced_on_read_too` mirrors it deterministically.
      Found while fixing it — a real defect in MY OWN earlier test: the rotation-durability test
      (r-series, directory fsync) captured `original = JsonlLogStore._fsync_dir`, which unwraps the
      staticmethod to a plain function, then restored THAT in its `finally`. Python rebinds a plain
      function assigned to a class as an ordinary method, so from that point on every
      `self._fsync_dir(directory)` in the session raised `TypeError: takes 1 positional argument
      but 2 were given` — rotation permanently broken for whatever ran next. Invisible in the full
      suite only because pytest's alphabetical order puts `test_log_operator_surface.py` BEFORE
      `test_log_persistence.py`; running the two files together in either order surfaced it, and
      the store's `finally: close()` masked the real error behind "flush of closed file". Now
      captures the DESCRIPTOR out of `__dict__`, and asserts after restore both that the shape is
      a `staticmethod` again and that a fresh store can still rotate. Checked the rest of the tree
      for the same restore-shape bug: the one sibling that patches a class attribute
      (`test_dashboard_provider.py`) targets a classmethod, which round-trips safely.
    r35 → BLOCK ×2.
      [high] "LOGS publisher drains old backlog before current events": the cursor started at
      `None`, which on an append-only trail means "publish everything retained". A deployment with
      real audit history would fan out old records 200 at a time while a kill-switch activation
      written a second after startup queued behind all of them — and every health signal stayed
      green: no read failure, no gap, events flowing. Delivering an alert far too late while
      reporting itself fine is the worst shape an alerting surface can take. FIXED: `start()` now
      seeds each class cursor at the trail's tail (`read_tail(limit=1)`; its positions are
      byte-identical to the forward scan's, verified), so the channel is what it is DECLARED to be
      — event-driven. A store that cannot be read is deliberately left unseeded so the first poll
      reports the failure through the normal path; a store that does not exist yet is left unseeded
      too, because everything it later receives is genuinely post-startup. The skipped history is
      declared on `health()["history_not_replayed"]` rather than left to be inferred — stated, not
      counted, since counting it means the unbounded scan r24 removed — and the full trail is still
      on `GET /api/v1/logs`. Regression `test_a_new_event_is_not_queued_behind_the_existing_history`
      (500-record backlog, nothing replayed, the post-start record arrives on the very next poll,
      and the REST query still reports all 501).
      Found while writing that test: `poll_once` was not serialised against itself, so the ticker
      and a direct caller both read the trail before either advanced the cursor and each published
      the same record — a duplicate audit line. Only the ticker calls it in production, but it is
      public and driven directly by tests and by any future flush-now path, so the invariant now
      lives in a `_poll_lock` rather than in an assumption about callers.
      [medium] "read_records launders wrong-class records through filters": r34 fixed
      `JsonlLogStore.read`, but the lock-free reader — the one the kill-switch status cell and the
      REL-001 availability CLI actually use — had the same hole, and both filter hard, so a
      contaminated trail would be filtered clean with no trace on the very surface that would have
      shown it. Added `expect_class` (checked before any filter) and passed it at both call sites;
      `killswitch.py` also had to widen its except tuple so a mismatch degrades to
      `KillSwitchStatusUnavailable` instead of crashing the cell. Regression pins BOTH states: the
      unasserted filtered read still hides it (the risk stays visible), the asserted one raises.
      `check_separation_is_enforced_on_read_too` now also greps the contract's
      `class_asserting_readers` map, so a new known-class reader cannot land without the assertion.
    r36 → WARN ×2 (reviewer: claude-fallback; codex rate-limited until 7:14 AM). Both were real and
      both are FIXED — no override taken.
      [warn] "unbounded rescan per poll": the anchor told the publisher WHERE it stopped, but it
      found that place by scanning from the head of the trail, so every 1s tick re-read, re-parsed
      and re-validated the whole retained history. Rotation is opt-in, so the cost only grows —
      until the ticker's real cadence drifts past its interval and `stop(timeout=5.0)` starts
      timing out, with `health()` still reporting `ok`. The same O(all-history) shape r24 removed
      from the pane, left in place on the publisher. FIXED with a physical resume:
      `record_at(path, position)` recovers the record occupying the anchor's slot in O(line) (a
      bounded backwards read, windows 1KiB→16KiB→64KiB so the common case costs ~1KiB), and
      `iter_records_with_positions(resume_after=...)` seeks past it and never opens older segments.
      The slot is verified by CONTENT as well as position, keeping the inode-reuse guard the
      forward scan had; when it fails, the publisher falls back to the full scan and reports an
      eviction gap rather than seeking past records nobody published. Measured, not asserted in
      prose: `test_a_poll_costs_the_page_not_the_whole_history` counts BYTES READ off the trail
      (must be under a quarter of it), `check_a_resumed_read_does_not_reopen_the_history` counts
      SEGMENTS OPENED (2 of 5), and `test_a_reused_slot_is_not_mistaken_for_the_resume_point`
      overwrites the anchor's line in place — same inode, same offsets, different content — and
      proves the record after it is still delivered.
      [warn] "--severity under-documents CRITICAL": `admin logs` is now a SERVED command whose
      manual asserts "the arguments above are the ones the live command honours", while the
      `--severity` summary listed only DEBUG/INFO/WARN/ERROR and the handler accepts all five.
      The missing one was CRITICAL — where kill-switch activations land — on the one CLI surface
      an operator uses to find critical audit events. Fixed in the declaration, regenerated into
      the frozen manual, and pinned by `check_the_cli_documents_every_severity_it_accepts`, which
      checks the summary against the `Severity` ENUM (not a copy of the list) in both the
      declaration and the frozen artefact.
    r37 → WARN ×3 (claude-fallback again; codex still limited). All three real, all three FIXED.
      [warn] "seed failure degrades into a history replay": `_seed_cursor` swallowed every read
      error and returned with the cursor unset — and an unset cursor means "publish everything
      retained". A transient failure (the rotation window where the active segment has been
      renamed but not yet recreated, EMFILE, EINTR) would therefore reach the r36 failure through
      its own error path: the next poll reads fine and replays the archive, no counter raised,
      `ok` still true. FIXED by making seeding a precondition rather than a best effort: it lives
      in the drain now (one path), returns success/failure, counts and surfaces a failure, and the
      poll SKIPS that class rather than falling through with no cursor. Writing the regression
      surfaced the mirror-image case: a trail that appears AFTER start has no history to skip, and
      seeding it to its tail would silently drop the operator's first records — so `start()`
      records which trails existed, and only those are skipped past.
      `test_a_failed_seed_never_degrades_into_replaying_the_archive` covers the transient path.
      [warn] "misleading corruption location on a resumed read": my own r36 edit. I replaced the
      corruption label with the offset-aware `where` using a count=1 `.replace()`, which hit the
      FIRST occurrence in the file — in `_read_segment`, not the streaming reader — and I then
      "fixed" the resulting NameError by reverting that one, leaving the streaming reader's JSON
      branch on `{path}:{lineno}`. On a resumed read `lineno` counts from the resume point, so a
      corruption incident on the publisher's steady-state path would have pointed an operator at
      the wrong line of the audit trail. Exactly the `.replace()`-anchor hazard the CI notes warn
      about, and worth recording because the sibling UTF-8 branch made it look done.
      [warn] "coverage claim incomplete": `SEQUENCE_GAP` is a declared `market_data` event type
      with no producer anywhere — `heartbeat.py` refuses to write one because IB's API exposes no
      tick sequence — and the coverage note accounted only for the other three. Rather than patch
      the string, fixed the granularity: `SourceCoverage` now declares `produced` event types and
      DERIVES the tri-state from them (`state_for`), the cell publishes
      `produced_event_types` / `unproduced_event_types`, the SPA chip shows the count and names
      the missing types on hover, and both the check and
      `test_coverage_is_stated_per_event_type_not_only_per_source` enforce that every declared
      type is accounted for and every unaccounted one names an owner. The derived state is
      asserted by mutation (flip `produced`, the verdict must move), so it cannot become a second
      copy that drifts. Owner for the gap is SRS-MD-007, and the note says it is unsatisfiable
      rather than merely unbuilt.
    r38 → two reviewer TIMEOUTS (block, zero findings — infrastructure, not a verdict; codex was
      rate-limited and the fallback could not finish a diff this size), then a real verdict on the
      third attempt: BLOCK + WARN + INFO, a single cluster in the SPA. All fixed.
      [block] "duplicate audit record": `renderLogs` deduped its side of the merge, `onLogEvent`
      prepended whatever it was handed. The REST poll and the LOGS channel carry the SAME records,
      so a record the snapshot had already delivered rendered a second time when its event arrived
      — on a surface whose job is to say what happened, one event shown twice reads as two
      occurrences. Guarded with the existing `logEventKey` (record_id), the other half of the
      merge that was already there.
      [warn] "order inversion": `pending.concat(snapshot)` assumed held-back events are always
      newer. Once a burst exceeds the pane's page an event falls off the newest-N window and no
      later snapshot can contain it again, so it would be pinned above strictly newer records
      permanently, in a table labelled newest-first. Now sorted by timestamp (stable, so ties keep
      delivery order).
      [info] "the harness never drove poll-then-event": true, and it was worse than that — adding
      the case did NOT fail, because the harness only reported the buffer after step 3, and step
      3's merge repaired the duplicate step 2 introduced. The duplicate is what an operator sees
      for up to a full poll interval. The harness now also reports the post-event frame, and
      "wrong for four seconds is still wrong" is why. Both new cases were mutation-verified: each
      fails with its own fix removed and passes with it restored.
      NOTE ON THE FINAL STATE: r38's fixes have NOT themselves been through an adversarial round —
      codex was still rate-limited and the operator called the session closed here. The
      deterministic critic approves, the full suite and all seven surface checks are green, and
      both new guards are mutation-verified, but the last SUBSTANTIVE adversarial pass covers the
      tree as of r37. `passes` stays false regardless.

  SELF-INFLICTED INCIDENT worth recording: while formatting after r6 I passed the STAGED FILE LIST
  (which includes .json) to `ruff format`. A JSON object is valid Python syntax, so ruff parsed
  `architecture/runtime_services.json` and `python/atp_ws/asyncapi.json` as Python dict literals and
  rewrote them with 4-space indent + magic trailing commas — invalid JSON, ~10k lines of churn.
  Caught immediately because every log check then failed to parse the contract. Recovered by
  restoring the contract from the index and regenerating the asyncapi snapshot from its own tool.
  RULE: only ever pass explicit .py paths to `ruff format` — never a file list that can contain
  .json.

  NOTE on a flaky unrelated test: one full-suite run showed
  `tests/test_unified_query_contract.py::test_srs_data_007_contract_script_passes` failing. It
  passes standalone, passes alongside the log suites, and `tools/unified_query_check.py` passes
  directly — the check shells a cargo-built binary, so this is the known concurrent-scratch-dir
  flake when a sibling agent runs cargo at the same time. Not caused by this change.

Known issues / notes for the next agent:
- SRS-LOG-001 stays passes:false. To FLIP it needs, in order of size:
  (1) PRODUCERS for the five unproduced sources — order_routing (SRS-EXE-001 live / SRS-EXE-002
      simulated), ingestion (SRS-DATA-001), container_lifecycle (SRS-ORCH-001), hot_swap
      (SRS-RESV-004 demotion / SRS-RESV-005 promotion), resource_monitor (SRS-ORCH-003) — plus the
      partial halves: IB Gateway CONNECT/DISCONNECT/RECONNECT (SRS-SAFE-003) and market-data
      SUBSCRIPTION_CHANGE (SRS-MD-001).
  (2) The browser-automation window: ATP_RUN_E2E=1 pytest tests/e2e/test_dashboard_logs.py.
  Everything those producers need already exists: import `atp_logging`, build a `LogRecord` with the
  right `Source`, and write it to the SYSTEM store the composer wired. The surfaces then show it
  with no further work.
- NOTE for whoever wires a producer: `tests/domain/test_log_operator_surface.py` FAILS when a
  producer lands without `atp_dashboard.logs.SOURCE_COVERAGE` being updated (it greps for modules
  that build `LogRecord`s naming that `Source`). That is deliberate — it stops the dashboard
  understating its own coverage forever. Update the map, the contract's
  `SRS-LOG-001-producer-coverage` deferred entry, and
  `test_five_of_the_eight_system_sources_still_have_no_producer` together.
- Two closed-green features carry an unclaimed SYS-61 gap worth flagging: SRS-ORCH-001 (container
  lifecycle) and SRS-ORCH-003 (SYS-58 resource threshold alerts) are both `passes:true` but emit no
  log records. Naming them in `block --on` does nothing (the scheduler only blocks on
  `passes:false` deps), so this is operator-visible work with no current owner.
- ENV: init.sh installs requirements.txt only; pytest/ruff/mypy/hypothesis were pip-installed into
  the worktree venv. `tools/run_ci_locally.sh` needs the venv on PATH. Its ruff-format gate reports
  8 PRE-EXISTING unformatted files (not this change; my files are format-clean). mypy reports 4
  PRE-EXISTING errors in `python/atp_orchestration/hot_swap_triggers.py` — verified byte-identical to
  origin/main.
Resume / next: nothing further to build on the LOG-001 SURFACE. The next useful move for this
  feature is to land a producer (SRS-RESV-004/005 are on the ready frontier and would take hot_swap
  from deferred → produced), then re-run the coverage guard. Do not rebuild the handler, publisher,
  pane, or wiring — they are integrated and contract-checked.
