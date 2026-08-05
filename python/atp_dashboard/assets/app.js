/* ATP · Mission Control — SRS-UI-001 dashboard client.
   Subscribes to the PNL / METRICS / HEARTBEAT WebSocket channels, polls the
   system snapshot for health + latency, and renders four panels refreshing
   inside the NFR-P2 5s budget. Vanilla ES2020, no dependencies, same-origin only.

   Honesty: a metric field arrives as {value, data_source}. value === null means
   the producer feature is still deferred; it renders as an explicit "—" with the
   owning feature tag — never a fabricated number. */
"use strict";

(function () {
  const BUDGET_MS = 5000;              // NFR-P2 dashboard-refresh budget
  const POLL_MS = 4000;                // system-snapshot poll (< 5s)
  const CIRC = 2 * Math.PI * 50;       // pulse gauge circumference (r=50)

  // ----- panel row schemas (label + numeric kind) ----------------------- //
  const ROWS = {
    pnl: [
      ["daily_pnl", "Daily P&L", "money"],
      ["cumulative_pnl", "Cumulative P&L", "money"],
      ["unrealized_pnl", "Unrealized P&L", "money"],
    ],
    metrics: [
      ["sharpe", "Sharpe", "ratio"],
      ["sortino", "Sortino", "ratio"],
      ["alpha", "Alpha", "ratio"],
      ["beta", "Beta", "ratio"],
      ["max_drawdown", "Max Drawdown", "pct"],
      ["benchmark_return", "Benchmark Δ (vs SPY)", "pct"],
    ],
    latency: [
      ["observed_refresh_ms", "Observed refresh", "ms"],
      ["refresh_budget_ms", "Refresh budget (NFR-P2)", "ms"],
      ["order_signal_to_ack_p95_ms", "Order → ack p95", "ms"],
      ["pipeline_fanout_p95_ms", "Pipeline fan-out p95", "ms"],
    ],
    health: [
      ["feed", "Market-data feed", "text"],
      ["staleness_seconds", "Heartbeat staleness", "sec"],
      ["is_stale", "Stale?", "bool"],
    ],
  };

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls) => { const n = document.createElement(tag); if (cls) n.className = cls; return n; };

  // ----- DOM construction ------------------------------------------------ //
  function buildRows(bodyId, rows, opts) {
    const body = $(bodyId);
    if (opts && opts.lead) body.appendChild(opts.lead);
    for (const [key, label] of rows) {
      const row = el("div", "metric");
      row.dataset.field = key;
      const l = el("span", "metric__label"); l.textContent = label;
      const right = el("span", "metric__right");
      const v = el("span", "metric__value is-deferred"); v.textContent = "—";
      const tag = el("span", "srctag"); tag.textContent = "…";
      right.append(v, tag);
      row.append(l, right);
      body.appendChild(row);
    }
    if (opts && opts.trail) body.appendChild(opts.trail);
  }

  function buildAll() {
    // Health panel leads with the readiness pill and trails with notes.
    const lead = el("div", "metric");
    lead.append(
      Object.assign(el("span", "metric__label"), { textContent: "Readiness gate" }),
      (() => { const r = el("span", "metric__right"); const p = el("span", "pill"); p.id = "health-pill"; p.textContent = "—"; r.appendChild(p); return r; })()
    );
    const notes = el("ul", "notelist"); notes.id = "health-notes";

    buildRows("body-pnl", ROWS.pnl, { lead: strategyLead("pnl") });
    buildRows("body-metrics", ROWS.metrics, { lead: strategyLead("metrics") });
    buildRows("body-health", ROWS.health, { lead: lead, trail: notes });
    buildRows("body-latency", ROWS.latency, {});
    // Budget is a real, known constant — fill it immediately.
    setField("body-latency", "refresh_budget_ms", { value: BUDGET_MS, data_source: "live" }, "ms");
    // Per-panel freshness indicator (driven by monitorFreshness).
    for (const panel of ["pnl", "metrics", "health", "latency", "strategies", "backtest", "account", "reservoir", "research", "alerts", "logs"]) addFreshDot(panel);
  }

  function addFreshDot(panel) {
    const meta = document.querySelector('[data-panel="' + panel + '"] .panel__meta');
    if (!meta) return;
    const dot = el("span", "freshdot");
    dot.id = "fresh-" + panel;
    dot.dataset.state = "wait";
    dot.title = "awaiting data";
    meta.insertBefore(dot, meta.firstChild);
  }

  function strategyLead(_panel) {
    const row = el("div", "metric");
    const l = el("span", "metric__label"); l.textContent = "Live strategy";
    const right = el("span", "metric__right");
    const v = el("span", "metric__value is-deferred"); v.textContent = "none";
    right.appendChild(v);
    row.append(l, right);
    row.dataset.meta = "strategy";
    return row;
  }

  // ----- value formatting ------------------------------------------------ //
  function fmt(kind, value) {
    if (typeof value !== "number" || !isFinite(value)) return String(value);
    switch (kind) {
      case "money": return (value / 100).toLocaleString(undefined, { style: "currency", currency: "USD" });
      case "pct": return (value * 100).toFixed(2) + "%";
      case "ratio": return value.toFixed(2);
      case "ms": return Math.round(value).toLocaleString() + " ms";
      case "sec": return value.toFixed(1) + " s";
      case "bool": return value ? "YES" : "NO";
      default: return String(value);
    }
  }

  // Update one metric row from a raw field (either {value,data_source} or scalar).
  function setField(bodyId, key, raw, kind) {
    const body = $(bodyId);
    const row = body && body.querySelector('[data-field="' + key + '"]');
    if (!row) return;
    const valEl = row.querySelector(".metric__value");
    const tagEl = row.querySelector(".srctag");

    let value = raw, source = "live";
    if (raw && typeof raw === "object" && "value" in raw) { value = raw.value; source = raw.data_source || "live"; }

    if (value === null || value === undefined) {
      valEl.textContent = "—";
      valEl.className = "metric__value is-deferred";
      if (tagEl) { tagEl.textContent = shortSource(source); tagEl.className = "srctag"; }
    } else {
      const changed = valEl.dataset.raw !== String(value);
      valEl.textContent = fmt(kind, value);
      valEl.dataset.raw = String(value);
      valEl.className = "metric__value" + directionClass(kind, value);
      if (tagEl) { tagEl.textContent = "live"; tagEl.className = "srctag srctag--live"; }
      if (changed) flash(row);
    }
  }

  function directionClass(kind, value) {
    if ((kind === "money" || kind === "pct" || kind === "ratio") && typeof value === "number") {
      if (value > 0) return " up";
      if (value < 0) return " down";
    }
    return "";
  }

  function shortSource(source) {
    if (typeof source !== "string") return "…";
    if (source.startsWith("deferred:")) return source.slice("deferred:".length);
    if (source === "client-measured") return "client";
    return source;
  }

  function flash(row) {
    row.classList.remove("flash");
    void row.offsetWidth; // restart the animation
    row.classList.add("flash");
  }

  // ----- per-channel freshness + SLA instrumentation --------------------- //
  // The ≤5s NFR-P2 SLA must hold for EACH required metric group, not "any
  // event": a 1s PNL/HEARTBEAT tick must NOT mask a stalled 5s METRICS/benchmark
  // panel. So freshness is tracked per channel and the gauge reflects the WORST
  // required channel's staleness — and a timer (not just events) drives it, so a
  // channel that goes SILENT still turns its panel + the gauge stale.
  const PANEL_FRESH = [
    { panel: "pnl", ch: "PNL", budget: 1000, gauge: true },
    { panel: "metrics", ch: "METRICS", budget: 5000, gauge: true },
    { panel: "health", ch: "HEARTBEAT", budget: 1000, gauge: true },
    { panel: "latency", ch: "SYSTEM", budget: POLL_MS, gauge: false },
    // SRS-UI-002 inventory: NOT part of the NFR-P2 gauge — the channel is a
    // composition-time opt-in (a bare SRS-UI-001 mount publishes no
    // STRATEGY_STATE and must not read as an SLA breach); the panel's own
    // freshness dot still reports it honestly.
    { panel: "strategies", ch: "STRATEGY_STATE", budget: 5000, gauge: false },
    // UI-3 backtest history: REST-poll (no WS channel), likewise off the gauge;
    // its dot tracks the /dashboard/api/backtests poll cadence.
    { panel: "backtest", ch: "BACKTEST", budget: POLL_MS, gauge: false },
    // SRS-UI-003 account + Reservoir: composition-time opt-in channels (a bare
    // SRS-UI-001 mount publishes neither and must not read as an SLA breach), so
    // they stay OFF the NFR-P2 gauge — each panel's own dot reports it honestly.
    { panel: "account", ch: "ACCOUNT_STATUS", budget: 5000, gauge: false },
    { panel: "reservoir", ch: "RESERVOIR_RANKING", budget: 5000, gauge: false },
    // SRS-RES-001 research embed: REST-poll (no WS channel), off the NFR-P2
    // gauge; its dot tracks the /dashboard/api/research poll cadence.
    { panel: "research", ch: "RESEARCH", budget: POLL_MS, gauge: false },
    // UI-1 critical alerts: NOT tracked here at all. While the SRS-NOTIF-001
    // producer is deferred, poll-cadence freshness would read as "alert
    // monitoring healthy" when only the placeholder route is healthy — so
    // renderAlerts() drives the pane's dot directly (wait/deferred, stale on
    // endpoint failure). Re-enter ALERTS here when the real feed lands.
  ];
  const STALE_GRACE_MS = 1500; // tolerate normal cadence jitter; flag real stalls
  const lastChannelAt = Object.create(null); // channel -> performance.now()
  let lastFrameAt = 0;

  function noteActivity(channel) {
    lastChannelAt[channel] = performance.now();
    lastFrameAt = Date.now();
    startCountdown();
    stamp();
  }

  function monitorFreshness() {
    const now = performance.now();
    let worst = 0;
    let gaugeReady = true;
    for (const { panel, ch, budget, gauge } of PANEL_FRESH) {
      const seen = lastChannelAt[ch] !== undefined;
      const staleness = seen ? now - lastChannelAt[ch] : Infinity;
      // "fresh" holds ONLY within budget; grace is a separate warn state so an
      // over-budget channel is never reported healthy (SRS-UI-001 / NFR-P2).
      markPanelFreshness(panel, freshnessState(staleness, budget, STALE_GRACE_MS));
      if (gauge) {
        if (!seen) gaugeReady = false;
        else worst = Math.max(worst, staleness);
      }
    }
    if (gaugeReady) {
      // Honest observed refresh = the WORST required channel's staleness vs the
      // 5s budget (not the fastest channel's inter-arrival).
      renderPulse(worst);
      setField(
        "body-latency", "observed_refresh_ms",
        { value: Math.round(worst), data_source: "client-measured" }, "ms"
      );
    }
  }

  const FRESH_TITLES = {
    wait: "awaiting data",
    fresh: "fresh — refreshing within budget",
    warn: "over budget (within jitter grace)",
    stale: "STALE — refresh contract violated",
  };

  function markPanelFreshness(panel, state) {
    const dot = $("fresh-" + panel);
    if (!dot) return;
    dot.dataset.state = state;
    dot.title = FRESH_TITLES[state] || state;
  }

  function renderPulse(observedMs) {
    const arc = $("pulse-arc");
    const frac = Math.max(0, Math.min(observedMs / BUDGET_MS, 1));
    arc.style.strokeDashoffset = String(CIRC * (1 - frac));
    // colour by headroom against the 5s budget
    const color = observedMs <= BUDGET_MS * 0.6 ? "var(--accent)" : observedMs <= BUDGET_MS ? "var(--warn)" : "var(--bad)";
    arc.style.stroke = color;
    $("pulse-value").textContent = Math.round(observedMs).toLocaleString();
    const p = document.querySelector(".pulse");
    p.classList.remove("tick"); void p.offsetWidth; p.classList.add("tick");
  }

  let countdownRAF = 0;
  function startCountdown() {
    cancelAnimationFrame(countdownRAF);
    const fill = $("refreshbar");
    const t0 = performance.now();
    const tick = (t) => {
      const frac = Math.min((t - t0) / BUDGET_MS, 1);
      fill.style.width = (frac * 100).toFixed(1) + "%";
      if (frac < 1) countdownRAF = requestAnimationFrame(tick);
    };
    countdownRAF = requestAnimationFrame(tick);
  }

  function stamp() {
    if (!lastFrameAt) return;
    const since = Date.now() - lastFrameAt;
    const s = Math.round(since / 1000);
    $("last-update").textContent = "last frame " + (s <= 0 ? "just now" : s + "s ago");
  }
  setInterval(stamp, 1000);
  setInterval(monitorFreshness, 500);

  // ----- WebSocket ------------------------------------------------------- //
  let ws = null;
  let backoff = 500;

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws/v1");
    setConn("connecting", "CONNECTING");

    ws.onopen = () => {
      backoff = 500;
      setConn("open", "LIVE");
      ws.send(JSON.stringify({ type: "SUBSCRIBE", channels: ["PNL", "METRICS", "HEARTBEAT", "STRATEGY_STATE", "ACCOUNT_STATUS", "RESERVOIR_RANKING", "LOGS"] }));
    };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (_e) { return; }
      if (msg.type !== "EVENT") return;
      onEvent(msg.channel, msg.data || {});
    };
    ws.onclose = () => {
      setConn("closed", "OFFLINE");
      backoff = Math.min(backoff * 2, 8000);
      setTimeout(connect, backoff);
    };
    ws.onerror = () => { try { ws.close(); } catch (_e) { /* onclose handles retry */ } };
  }

  // ----- SRS-MD-003 heartbeat freshness (per-feed keyed rows) -------------- //
  // Rendered above the health notes; a stale feed (or one with NO data — the
  // fail-closed never-observed state) shows a red "stale" pill, a fresh feed a
  // green "fresh" pill, with the observed staleness age alongside.
  function upsertHeartbeatRow(data) {
    const body = $("body-health");
    const feedKey = data.feed;
    let row = null;
    for (const child of body.children) {
      if (child.dataset && child.dataset.hbFeed === feedKey) { row = child; break; }
    }
    if (!row) {
      row = el("div", "metric");
      row.dataset.hbFeed = feedKey;
      const l = el("span", "metric__label"); l.textContent = feedKey;
      const right = el("span", "metric__right");
      const v = el("span", "metric__value");
      const pill = el("span", "pill");
      right.append(v, pill);
      row.append(l, right);
      const notes = $("health-notes");
      if (notes && notes.parentNode === body) body.insertBefore(row, notes);
      else body.appendChild(row);
    }
    const v = row.querySelector(".metric__value");
    const pill = row.querySelector(".pill");
    const stale = data.is_stale === true;
    const secs = data.staleness_seconds;
    v.textContent = (secs === null || secs === undefined)
      ? "no data"
      : `${Number(secs).toFixed(1)} s`;
    v.classList.toggle("is-deferred", secs === null || secs === undefined);
    pill.textContent = stale ? "stale" : "fresh";
    pill.className = stale ? "pill pill--bad" : "pill pill--ok";
  }

  function onEvent(channel, data) {
    if (channel === "PNL") {
      applyMeta("body-pnl", data.strategy_id);
      for (const [k, , kind] of ROWS.pnl) setField("body-pnl", k, data[k], kind);
    } else if (channel === "METRICS") {
      applyMeta("body-metrics", data.strategy_id);
      for (const [k, , kind] of ROWS.metrics) setField("body-metrics", k, data[k], kind);
    } else if (channel === "HEARTBEAT") {
      if (data && typeof data.feed === "string") {
        // SRS-MD-003 live monitor mounted: one event PER FEED (market-data
        // lines + ib_gateway). Upsert a keyed row per feed so feeds never
        // overwrite each other's cells.
        upsertHeartbeatRow(data);
      } else {
        // Deferred composition (no observation source): the static cells.
        for (const [k, , kind] of ROWS.health) setField("body-health", k, data[k], kind);
      }
    } else if (channel === "STRATEGY_STATE") {
      onInventoryEvent(data);
    } else if (channel === "ACCOUNT_STATUS") {
      onAccountEvent(data);
    } else if (channel === "RESERVOIR_RANKING") {
      renderReservoir(data);
    } else if (channel === "LOGS") {
      // SRS-LOG-001: one event per newly-persisted record, routed to its own
      // class buffer by the record's own log_class discriminant — the WS path
      // cannot merge the two trails any more than the REST path can.
      onLogEvent(data);
    }
    noteActivity(channel);
  }

  // ----- SRS-UI-002 strategy inventory ------------------------------------ //
  // One summary event + one event per strategy per tick. Rows are keyed by
  // strategy_id; a cell arriving as {value:null, data_source:"deferred:<owner>"}
  // renders as the explicit "—" with the owning feature tag — never fabricated.
  function inventoryCell(raw) {
    const td = el("td");
    let value = raw, source = "live";
    if (raw && typeof raw === "object" && "value" in raw) { value = raw.value; source = raw.data_source || "live"; }
    if (value === null || value === undefined) {
      const v = el("span", "metric__value is-deferred"); v.textContent = "—";
      const tag = el("span", "srctag"); tag.textContent = shortSource(source);
      td.append(v, tag);
    } else {
      const v = el("span", "metric__value"); v.textContent = String(value);
      td.appendChild(v);
      if (typeof source === "string" && source.startsWith("live")) {
        const tag = el("span", "srctag srctag--live"); tag.textContent = "live";
        td.appendChild(tag);
      }
    }
    return td;
  }

  // Row lifecycle: every summary event starts a new inventory generation and
  // every rendered row is stamped with it. Once the generation's expected row
  // events have all arrived, rows the current inventory no longer contains are
  // REMOVED (armed state disarmed first) — a strategy that left the inventory
  // must not keep an actionable PROMOTE LIVE row. Zero-strategy and
  // unavailable summaries clear the table immediately (fail-closed: unknown
  // truth never keeps stale actionable rows).
  // The WS STRATEGY_STATE feed and the REST poll are TWO independent burst
  // sources over the same truth: each keeps its own open-summary state (so a
  // delayed frame from one source can never read as corruption of the other's
  // burst — only a source contradicting its OWN summary is malformed), while
  // rows are stamped with one GLOBAL monotonic generation so completed bursts
  // sweep only rows older than themselves.
  let inventoryGen = 0; // global, monotonic across both sources
  const inventorySources = {
    ws: { gen: 0, expected: null, seen: 0 },
    rest: { gen: 0, expected: null, seen: 0 },
  };
  // SRS-ORCH-005 capability, reported by the server on every inventory summary:
  // whether THIS runtime serves the lifecycle rollback route. Starts FALSE and
  // only an explicit boolean true from a summary sets it — an absent, stringy,
  // or version-skewed field leaves the control inert (an unproven capability is
  // not a capability). Never inferred from the row data.
  let rollbackAvailable = false;

  function removeInventoryRow(tr) {
    if (promoteArmedId === tr.dataset.strategy) disarmPromote(true);
    // A strategy that left the inventory must not keep an actionable ROLLBACK
    // either — the staged target hash it was armed against is no longer
    // substantiated by any row.
    if (rollbackArmedId === tr.dataset.strategy) disarmRollback(true);
    tr.remove();
  }

  function sweepStaleInventoryRows(minGen) {
    const rows = $("inventory-rows");
    if (!rows) return;
    rows.querySelectorAll("tr").forEach((tr) => {
      if (Number(tr.dataset.gen) < minGen) removeInventoryRow(tr);
    });
    if (!rows.children.length) $("inventory-table").hidden = true;
  }

  function clearInventoryRows() {
    const rows = $("inventory-rows");
    if (!rows) return;
    rows.querySelectorAll("tr").forEach(removeInventoryRow);
    $("inventory-table").hidden = true;
  }

  function renderInventoryRow(data, sourceKey) {
    const rows = $("inventory-rows");
    if (!rows) return;
    const src = inventorySources[sourceKey || "ws"];
    // Rows are truth only inside THIS source's open healthy summary: with no
    // open summary (unavailable / never summarized) a delayed or stray frame
    // is dropped, and a row beyond the source's own declared count means the
    // burst contradicts its own summary — unknown truth, fail closed. Either
    // way a stale frame must never resurrect an actionable PROMOTE LIVE row.
    if (src.expected === null) return;
    if (src.seen >= src.expected) {
      inventoryUnavailable("inventory unavailable: more rows than the summary declared");
      return;
    }
    const key = String(data.strategy_id);
    // A data refresh voids any staged confirmation for this row — an armed
    // button must never survive a row rebuild and fire against renewed data —
    // and the designation readout returns to resting (a stale "armed" caption
    // with nothing staged would misstate the control's state).
    if (promoteArmedId === key) disarmPromote(true);
    // Same for rollback: the retained previous version is re-read on every
    // refresh, so an armed control must never survive a rebuild and fire
    // against a target hash the new data no longer reports.
    if (rollbackArmedId === key) disarmRollback(true);
    let tr = rows.querySelector('[data-strategy="' + CSS.escape(key) + '"]');
    if (!tr) { tr = el("tr"); tr.dataset.strategy = key; rows.appendChild(tr); }
    tr.textContent = "";
    const name = el("td", "inventory__name"); name.textContent = String(data.name || key);
    tr.appendChild(name);
    tr.appendChild(inventoryCell(data.mode));
    tr.appendChild(inventoryCell(data.asset_class));
    tr.appendChild(inventoryCell(data.container_status));
    tr.appendChild(inventoryCell(data.version_identifier || data.deployment_version_hash));
    tr.appendChild(inventoryCell(data.pnl));
    tr.appendChild(inventoryCell(data.position_count));
    const manage = el("td", "inventory__manage");
    const btn = el("button", "manage__btn");
    btn.type = "button";
    btn.dataset.armed = "false";
    btn.dataset.strategy = key;
    btn.textContent = "PROMOTE LIVE";
    // SRS-ORCH-005: the rollback TARGET is the retained PREVIOUS version, not
    // the current one. The snapshot reports it as null when the strategy has
    // never been redeployed — SYS-80 rollback is INERT before a second
    // deployment, so the control is disabled rather than posting a request the
    // gate would refuse NO_PREVIOUS_VERSION. A row whose previous version is
    // unknown never presents an actionable rollback.
    // TWO independent conditions must BOTH hold for an actionable rollback:
    // (1) this runtime actually serves the rollback route (rollbackAvailable,
    // reported by the server — a retained previous version in the data says
    // nothing about whether the handler is mounted), and (2) the strategy has a
    // retained previous version to roll back TO. Either one missing renders the
    // control inert, so a dashboard composed without the SRS-ORCH-005 handler
    // never presents a control that would post into a 501.
    const previous = cellValue(data.previous_version_identifier);
    const targetHash = previous === null || previous === undefined
      ? "" : String(previous).split("@", 1)[0];
    // ...and never while an ambiguous outcome is held: rows re-render every 5 s,
    // so without this the rebuild would visually re-arm the control mid-hold.
    // (The click guard already blocks the POST, but a button that LOOKS
    // actionable while the deployed version is unverified is exactly the
    // stale-truth-left-actionable failure this control is guarding against.)
    const actionable =
      rollbackAvailable === true && targetHash !== "" && !rollbackAmbiguous;
    // Deliberately NOT .manage__btn: that class is the promote control's
    // identity in the UI-2 selectors/tests. The two controls share every style
    // rule (see styles.css — NFR-S2 parity is about the affordance, not the
    // class name) while staying independently addressable.
    const rb = el("button", "rollback__btn");
    rb.type = "button";
    rb.dataset.armed = "false";
    rb.dataset.strategy = key;
    rb.dataset.target = actionable ? targetHash : "";
    rb.textContent = "ROLLBACK";
    if (!actionable) {
      rb.disabled = true;
      rb.title = rollbackAmbiguous
        ? "a previous rollback's outcome is unverified — held until the refresh resolves it"
        : rollbackAvailable !== true
          ? "rollback route not served by this dashboard — SRS-ORCH-005 handler not composed"
          : "no retained previous version — nothing to roll back to";
    }
    const cd = el("span", "manage__cd"); cd.setAttribute("aria-hidden", "true");
    manage.append(btn, rb, cd);
    tr.appendChild(manage);
    tr.dataset.gen = String(src.gen);
    $("inventory-table").hidden = false;
    src.seen += 1;
    if (src.seen >= src.expected) {
      // This source's burst is complete: rows older than it are no longer in
      // the inventory. Rows stamped by a NEWER burst (the other source
      // superseded this one mid-flight) are left alone.
      sweepStaleInventoryRows(src.gen);
    }
  }

  function closeInventorySources() {
    inventorySources.ws.expected = null;
    inventorySources.rest.expected = null;
  }

  function onInventoryEvent(data, sourceKey) {
    const summary = $("inventory-summary");
    const src = inventorySources[sourceKey || "ws"];
    if (data.event === "inventory-summary") {
      if (!summary) return;
      const n = Number(data.strategy_count);
      if (data.ok === false || data.ok !== true || !Number.isInteger(n) || n < 0) {
        // Unknown truth — an unreadable source (ok:false) and a malformed or
        // version-skewed summary (ok not exactly true, or a count that is not
        // a non-negative integer) both fail closed: clear the rows too; stale
        // actionable PROMOTE LIVE rows must not survive an error caption.
        clearInventoryRows();
        closeInventorySources();
        // An unreadable/malformed source cannot substantiate the capability
        // either — drop it so no stale actionable rollback survives the error.
        rollbackAvailable = false;
        summary.textContent = "inventory unavailable: " +
          (data.ok === false ? String(data.error || "unknown") : "malformed summary");
        summary.dataset.tone = "error";
      } else {
        rollbackAvailable = data.rollback_available === true;
        // Release an ambiguous hold ONLY on a refresh that could not have raced
        // the request: one arriving after the server's own deadline for the
        // operation. A poll that lands mid-handler would report the
        // pre-rollback version and masquerade as proof of a terminal state.
        if (rollbackAmbiguous && Date.now() >= rollbackHoldUntilMs) {
          rollbackAmbiguous = false;
          rollbackInFlight = false;
          rollbackHoldUntilMs = 0;
          rollbackStatus(ROLLBACK_RESTING, "resting");
        }
        inventoryGen += 1;
        src.gen = inventoryGen;
        src.expected = n;
        src.seen = 0;
        if (n === 0) clearInventoryRows();
        summary.textContent = n === 0
          ? "no strategies deployed"
          : n + " strateg" + (n === 1 ? "y" : "ies") + " · deployed version live · other cells await their producer features";
        summary.dataset.tone = "ok";
      }
      return;
    }
    if (data.strategy_id) renderInventoryRow(data, sourceKey || "ws");
  }

  // ----- UI-2 promote-live control (SYS-2c / NFR-S2 / AC-15) -------------- //
  // Two-step arm-then-confirm against the CONTRACT route on this same runtime
  // (never a /dashboard path): the arm click stages exactly one candidate, the
  // confirm click POSTs, and the rendered outcome is the runtime's own
  // response, verbatim. While the SRS-EXE-001 designation handler is deferred
  // the runtime answers 501 HANDLER_DEFERRED and that is exactly what the
  // operator sees — a refusal is never dressed as success, and no POST outcome
  // ever marks a row or Mode cell "live" (that cell's producer is the durable
  // designation state, not this control).
  const PROMOTE_ARM_WINDOW_MS = 5000;
  const PROMOTE_FETCH_TIMEOUT_MS = 15000;
  const PROMOTE_LIVE_RESTING = "live designation state — awaits SRS-EXE-001";
  function promoteLiveRoute(id) {
    return "/api/v1/strategies/" + encodeURIComponent(id) + "/promote-live?confirm=true";
  }
  let promoteArmedId = null;
  let promoteArmTimer = null;
  let promoteInFlight = false; // one designation request at a time (AC-15)

  function designationStatus(text, tone) {
    const wrap = $("designation-state"), cap = $("designation-status");
    if (!wrap || !cap) return;
    cap.textContent = text;
    wrap.dataset.state = tone;
  }

  function disarmPromote(restoreResting) {
    if (promoteArmTimer) { clearTimeout(promoteArmTimer); promoteArmTimer = null; }
    promoteArmedId = null;
    const table = $("inventory-table");
    if (table) table.classList.remove("manage-staging");
    const rows = $("inventory-rows");
    if (rows) {
      rows.querySelectorAll("tr.manage-armed").forEach((tr) => tr.classList.remove("manage-armed"));
      rows.querySelectorAll('.manage__btn[data-armed="true"]').forEach((armed) => {
        armed.dataset.armed = "false";
        armed.textContent = "PROMOTE LIVE";
      });
    }
    if (restoreResting) designationStatus(PROMOTE_LIVE_RESTING, "deferred");
  }

  function armPromote(btn, id) {
    disarmPromote(false); // exactly one staged candidate at a time
    // ...and never a staged rollback alongside it. Restore the rollback caption
    // to resting ONLY if something was actually staged there: a prior REFUSED /
    // confirmed outcome is still true and must not be wiped by arming a
    // different control, but a stale "armed:" caption with nothing staged would
    // misstate the control's state.
    disarmRollback(rollbackArmedId !== null);
    promoteArmedId = id;
    btn.dataset.armed = "true";
    btn.textContent = "CONFIRM LIVE: " + id + "?";
    const tr = btn.closest("tr");
    if (tr) tr.classList.add("manage-armed");
    const table = $("inventory-table");
    if (table) table.classList.add("manage-staging");
    designationStatus("armed: " + id + " — confirm within 5s to request live designation", "armed");
    promoteArmTimer = setTimeout(() => { disarmPromote(true); }, PROMOTE_ARM_WINDOW_MS);
  }

  async function firePromote(btn, id) {
    disarmPromote(false);
    promoteInFlight = true; // every promote control is inert until this settles
    btn.disabled = true;
    designationStatus("requesting live designation: " + id + "…", "pending");
    try {
      const res = await fetch(promoteLiveRoute(id), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
        signal: AbortSignal.timeout(PROMOTE_FETCH_TIMEOUT_MS),
      });
      let body = null;
      try { body = await res.json(); } catch (error) { body = null; }
      if (res.ok) {
        // A 200 renders the runtime's OWN response fields — and only an
        // explicit boolean true FOR THE CONFIRMED STRATEGY reads as
        // designated (fail-closed parse; a missing or stringy is_live, or a
        // response naming a different strategy_id, must never read as live —
        // the NFR-S2 confirmation is bound to one exact strategy).
        if (body && body.is_live === true && String(body.strategy_id) === id) {
          designationStatus("runtime confirmed live designation: " + String(body.strategy_id) +
            " @ " + String(body.promoted_at), "live");
        } else if (body && body.is_live === true) {
          designationStatus("runtime answered " + res.status + " for strategy_id " +
            String(body.strategy_id) + " ≠ confirmed " + id + " — NOT designated", "error");
        } else {
          designationStatus("runtime answered " + res.status + " without is_live=true — " +
            id + " NOT designated", "error");
        }
      } else {
        const err = body && body.error ? body.error : {};
        const type = String(err.type || err.category || "UNKNOWN");
        const owner = err.detail && err.detail.owner ? " (owner " + String(err.detail.owner) + ")" : "";
        designationStatus("REFUSED " + res.status + " " + type + owner + ": " + id + " not designated", "error");
      }
    } catch (error) {
      designationStatus("FAILED: " + String(error) + " — designation outcome unknown", "error");
    }
    promoteInFlight = false;
    btn.disabled = false;
  }

  // ----- SRS-ORCH-005 rollback control (SYS-80 / NFR-S2) ------------------ //
  // The AC requires rollback of the live strategy to use "the same confirmation
  // control as live promotion", so this is deliberately the SAME two-step
  // arm-then-confirm affordance above — same window, same in-flight
  // serialization, same fail-closed rendering — pointed at the CONTRACT
  // lifecycle route mount_rollback serves on this same runtime.
  //
  // The two controls are MUTUALLY EXCLUSIVE: both mutate live deployment state,
  // so arming either disarms the other and while EITHER request is in flight
  // every control is inert. Two staged live-state mutations at once would leave
  // the operator unable to say which one a response belongs to.
  const ROLLBACK_ARM_WINDOW_MS = 5000;
  // MUST exceed the server's rollback deadline (atp_orchestration's
  // _DEFAULT_TIMEOUT_S = 30 s for the CLI subprocess). Aborting the browser
  // fetch does NOT cancel the handler, so a client timeout shorter than the
  // server's would report "outcome unknown" and re-arm the control while an
  // IRREVERSIBLE rollback was still completing server-side.
  const ROLLBACK_FETCH_TIMEOUT_MS = 35000;
  const ROLLBACK_RESTING = "rollback — none requested this session";
  // NFR-S2 parity is literal: the operator's confirm act rides in the SAME
  // place promote-live puts it — the `confirm` token on the contract route.
  // (The runtime's action-level guard 428s a `rollback` action without it, and
  // the handler re-checks request.confirmed behind that.)
  function rollbackRoute(id) {
    return "/api/v1/strategies/" + encodeURIComponent(id) + "/lifecycle?confirm=true";
  }
  let rollbackArmedId = null;
  let rollbackArmTimer = null;
  let rollbackInFlight = false;
  // Set when a rollback request's outcome is UNKNOWN (timeout / transport
  // failure). The server may still be completing an irreversible action, so
  // every control stays inert until a healthy inventory summary re-reads the
  // deployed version and resolves it. Never cleared by a retry.
  let rollbackAmbiguous = false;
  // Wall-clock instant before which no inventory refresh can prove the server
  // finished (see holdRollbackAmbiguous). Matches the handler's own subprocess
  // budget — atp_orchestration._DEFAULT_TIMEOUT_S = 30 s.
  const ROLLBACK_SERVER_DEADLINE_MS = 30000;
  let rollbackHoldUntilMs = 0;
  // Refusal types the GATE raises strictly BEFORE its single registry write, so
  // the deployed state is provably untouched and the control may safely re-arm.
  // Everything else — an unevidenced 2xx, a subprocess timeout, unparseable
  // output, an unknown/absent type — leaves the durable outcome UNKNOWN and
  // must hold. Allow-list, not deny-list: a refusal type this dashboard has
  // never heard of is treated as ambiguous, not as safe.
  const ROLLBACK_PRE_WRITE_REFUSALS = new Set([
    "LIVE_ROLLBACK_UNCONFIRMED", "CONFIRMATION_REQUIRED",
    "NEVER_DEPLOYED", "NO_PREVIOUS_VERSION",
    "TARGET_MISMATCH", "TARGET_HASH_INVALID",
    "CONFIRMATION_STRATEGY_MISMATCH", "LIVE_STATUS_UNAVAILABLE",
    "MISSING_STRATEGY_ID", "MISSING_TARGET_VERSION_HASH",
    "ROLLBACK_CLI_LAUNCH_FAILED", // never launched -> nothing was written
    "HANDLER_DEFERRED",           // 501: no handler ran at all
  ]);

  function holdRollbackAmbiguous(message) {
    // The durable outcome is unknown: the server may still be completing an
    // irreversible rollback. Hold every control.
    //
    // A healthy summary alone does NOT release it: an inventory poll that
    // lands WHILE the handler is still running reports the pre-rollback
    // version and would look like proof of a terminal state. The hold can only
    // be released by a refresh that could not have raced the request — i.e.
    // one read after the server's own deadline for the operation has elapsed,
    // by which point the handler has terminated its subprocess and the
    // snapshot is final either way.
    rollbackAmbiguous = true;
    rollbackHoldUntilMs = Date.now() + ROLLBACK_SERVER_DEADLINE_MS;
    rollbackStatus(message + " — the deployed version is UNVERIFIED; controls held " +
      "until the inventory refresh resolves it.", "error");
  }

  // One shared gate for both live-state controls (see mutual exclusion above).
  function controlsBusy() { return promoteInFlight || rollbackInFlight || rollbackAmbiguous; }

  function rollbackStatus(text, tone) {
    const wrap = $("rollback-state"), cap = $("rollback-status");
    if (!wrap || !cap) return;
    cap.textContent = text;
    wrap.dataset.state = tone;
  }

  function disarmRollback(restoreResting) {
    if (rollbackArmTimer) { clearTimeout(rollbackArmTimer); rollbackArmTimer = null; }
    rollbackArmedId = null;
    const table = $("inventory-table");
    if (table) table.classList.remove("manage-staging");
    const rows = $("inventory-rows");
    if (rows) {
      rows.querySelectorAll("tr.rollback-armed").forEach((tr) => tr.classList.remove("rollback-armed"));
      rows.querySelectorAll('.rollback__btn[data-armed="true"]').forEach((armed) => {
        armed.dataset.armed = "false";
        armed.textContent = "ROLLBACK";
      });
    }
    if (restoreResting) rollbackStatus(ROLLBACK_RESTING, "resting");
  }

  function armRollback(btn, id, target) {
    disarmRollback(false);
    // Exactly one staged live-state mutation at a time. Same asymmetry as
    // armPromote: only a genuinely-armed promote gets its caption reset.
    disarmPromote(promoteArmedId !== null);
    rollbackArmedId = id;
    btn.dataset.armed = "true";
    btn.textContent = "CONFIRM ROLLBACK: " + id + "?";
    const tr = btn.closest("tr");
    if (tr) tr.classList.add("rollback-armed");
    const table = $("inventory-table");
    if (table) table.classList.add("manage-staging");
    rollbackStatus("armed: " + id + " → " + target +
      " — confirm within 5s to roll back to the retained previous version", "armed");
    rollbackArmTimer = setTimeout(() => { disarmRollback(true); }, ROLLBACK_ARM_WINDOW_MS);
  }

  async function fireRollback(btn, id, target) {
    disarmRollback(false);
    rollbackInFlight = true; // every control is inert until this settles
    btn.disabled = true;
    rollbackStatus("requesting rollback: " + id + " → " + target + "…", "pending");
    try {
      const res = await fetch(rollbackRoute(id), {
        method: "POST",
        headers: { "content-type": "application/json" },
        // The rollback TARGET is the retained previous version: the gate
        // refuses any other hash (TARGET_MISMATCH), so naming it here is what
        // makes the request specific rather than "roll back to whatever".
        body: JSON.stringify({
          action: "rollback",
          target_version_hash: target,
        }),
        signal: AbortSignal.timeout(ROLLBACK_FETCH_TIMEOUT_MS),
      });
      let body = null;
      try { body = await res.json(); } catch (error) { body = null; }
      if (res.ok) {
        // Fail-closed parse, exactly as the promote control does: a 2xx alone
        // proves nothing rolled back. The response must name the strategy the
        // operator confirmed AND carry the restored version hash — a body
        // naming a different strategy_id, a missing/blank
        // deployment_version_hash, or a lifecycle_state that is not
        // "rolled-back" all render UNRESOLVED, never success. A rollback the
        // runtime cannot evidence must never read as one that happened.
        const named = body ? String(body.strategy_id) : "";
        const restored = body && body.deployment_version_hash
          ? String(body.deployment_version_hash) : "";
        // The restored version must be EXACTLY the target the operator armed
        // against. A response naming the right strategy but a different hash is
        // not the rollback that was confirmed — rendering it as success would
        // defeat the whole point of a target-specific rollback.
        if (body && named === id && restored === target
            && body.lifecycle_state === "rolled-back") {
          rollbackStatus("runtime confirmed rollback: " + named + " restored to " + restored +
            (body.was_live === true ? " (was LIVE — confirmed)" : ""), "live");
        } else if (body && named && named !== id) {
          // A 2xx is the server saying it DID something. If it cannot be
          // correlated to what the operator confirmed, the durable state is
          // unknown — exactly as ambiguous as a timeout, and it must hold.
          holdRollbackAmbiguous("runtime answered " + res.status + " for strategy_id " +
            named + " ≠ confirmed " + id);
          btn.disabled = true;
          return;
        } else if (body && named === id && restored && restored !== target) {
          holdRollbackAmbiguous("runtime answered " + res.status + " restoring " + restored +
            " ≠ confirmed target " + target);
          btn.disabled = true;
          return;
        } else {
          holdRollbackAmbiguous("runtime answered " + res.status +
            " without an evidenced rolled-back version for " + id);
          btn.disabled = true;
          return;
        }
      } else {
        const err = body && body.error ? body.error : {};
        const type = String(err.type || err.category || "UNKNOWN");
        const reason = err.detail && err.detail.reason ? " [" + String(err.detail.reason) + "]" : "";
        if (!ROLLBACK_PRE_WRITE_REFUSALS.has(type)) {
          // Not a known pre-write gate refusal (a CLI timeout, unparseable
          // output, an unknown type…): the binary may have run and written.
          holdRollbackAmbiguous("runtime answered " + res.status + " " + type + reason +
            " for " + id + ", which is not a known pre-write refusal");
          btn.disabled = true;
          return;
        }
        rollbackStatus("REFUSED " + res.status + " " + type + reason + ": " + id +
          " not rolled back", "error");
      }
    } catch (error) {
      // AMBIGUOUS: aborting the fetch does not cancel the server's handler, so
      // the irreversible rollback may still be completing. Do NOT re-arm — hold
      // every control inert until a fresh inventory summary re-reads the real
      // deployed version from the snapshot and resolves what actually happened.
      // (If the inventory feed is also down, the control stays inert — the
      // fail-closed direction.)
      holdRollbackAmbiguous("FAILED: " + String(error) + " — the request may still be " +
        "completing server-side");
      btn.disabled = true;
      return; // leaves rollbackInFlight true; cleared by the next healthy summary
    }
    rollbackInFlight = false;
    btn.disabled = false;
  }

  (function bindPromoteControls() {
    const rows = $("inventory-rows");
    if (!rows) return;
    rows.addEventListener("click", (event) => {
      // Serialize at the UI boundary: while one live-state request is in
      // flight EVERY control (promote AND rollback) is inert — competing
      // requests whose responses race would break the one-live invariant's
      // operator story (AC-15 / NFR-S2 / SYS-80).
      if (controlsBusy()) return;
      const btn = event.target.closest(".manage__btn, .rollback__btn");
      if (!btn || btn.disabled) return;
      const id = String(btn.dataset.strategy || "");
      if (!id) return;
      if (btn.classList.contains("rollback__btn")) {
        const target = String(btn.dataset.target || "");
        if (!target) return; // inert without a retained previous version
        if (btn.dataset.armed !== "true") { armRollback(btn, id, target); return; }
        fireRollback(btn, id, target);
        return;
      }
      if (btn.dataset.armed !== "true") { armPromote(btn, id); return; }
      firePromote(btn, id);
    });
  })();

  function applyMeta(bodyId, strategyId) {
    const row = $(bodyId).querySelector('[data-meta="strategy"] .metric__value');
    if (!row) return;
    if (strategyId) { row.textContent = strategyId; row.classList.remove("is-deferred"); }
    else { row.textContent = "none"; row.classList.add("is-deferred"); }
  }

  // Unwrap a {value, data_source} cell to its bare value (or pass a scalar through).
  function cellValue(raw) {
    return (raw && typeof raw === "object" && "value" in raw) ? raw.value : raw;
  }

  // ----- SRS-UI-003 account-level IB status (ACCOUNT_STATUS) -------------- //
  // Total IB account equity, daily/cumulative P&L, margin usage, buying power,
  // and IB connection state "as reported by the IB account". Every field is an
  // honest deferred cell until SRS-EXE-006 (live IB) lands — never fabricated.
  const ACCOUNT_FIELDS = [
    ["equity", "money"],
    ["daily_pnl", "money"],
    ["cumulative_pnl", "money"],
    ["margin_usage", "pct"],
    ["buying_power", "money"],
  ];

  function onAccountEvent(data) {
    for (const [k, kind] of ACCOUNT_FIELDS) setField("body-account", k, data[k], kind);
    renderMarginMeter(data.margin_usage);
    renderConnPill(data.ib_connection_state);
  }

  function renderMarginMeter(raw) {
    const fill = $("account-margin-fill");
    if (!fill) return;
    const value = cellValue(raw);
    if (typeof value === "number" && isFinite(value)) {
      const frac = Math.max(0, Math.min(value, 1));
      fill.style.width = (frac * 100).toFixed(1) + "%";
      fill.dataset.state = frac >= 0.9 ? "bad" : frac >= 0.6 ? "warn" : "ok";
    } else {
      fill.style.width = "0%";
      fill.dataset.state = "deferred";
    }
  }

  function renderConnPill(raw) {
    const pill = $("account-conn-pill");
    if (!pill) return;
    const cell = pill.closest('[data-field="ib_connection_state"]');
    const tag = cell && cell.querySelector(".srctag");
    let value = raw, source = "live";
    if (raw && typeof raw === "object" && "value" in raw) { value = raw.value; source = raw.data_source || "live"; }
    if (value === null || value === undefined) {
      pill.textContent = "awaiting";
      pill.dataset.state = "deferred";
      if (tag) { tag.textContent = shortSource(source); tag.className = "srctag"; }
    } else {
      const s = String(value).toUpperCase();
      pill.textContent = s;
      pill.dataset.state = /CONNECT|UP|READY|OK/.test(s) ? "ok" : /DISCON|DOWN|LOST|ERROR|FAIL/.test(s) ? "bad" : "warn";
      if (tag) { tag.textContent = "live"; tag.className = "srctag srctag--live"; }
    }
  }

  // ----- SRS-UI-003 Reservoir overview (RESERVOIR_RANKING) --------------- //
  // Paper-strategy ranking (Sharpe / Sortino / momentum) over the SYS-48 shared
  // evaluation window. The window control is REAL (SYS-48 constants); the
  // ranking output is deferred to SRS-RESV-002 — a deferred `rankings` cell
  // renders as an honest "awaiting" summary, NOT an empty "0 strategies" table.
  const RESV_WINDOWS = [1, 7, 15, 30, 60, 90]; // SYS-48 fallback if route unmounted
  const RESV_DEFAULT = 30;                       // SYS-48 default
  let resvWindow = RESV_DEFAULT;
  let resvLast = { rankings: { value: null, data_source: "deferred:SRS-RESV-002" } };

  function buildReservoirWindows(windows, dflt) {
    const sel = $("resv-window");
    if (!sel) return;
    const list = (Array.isArray(windows) && windows.length ? windows : RESV_WINDOWS).map(Number);
    const want = list.join(",");
    if (sel.dataset.windows === want) return; // already built with this option set
    const keep = sel.value ? Number(sel.value) : (typeof dflt === "number" ? dflt : RESV_DEFAULT);
    sel.dataset.windows = want;
    sel.textContent = "";
    for (const w of list) {
      const opt = el("option"); opt.value = String(w); opt.textContent = w + (w === 1 ? " day" : " days");
      sel.appendChild(opt);
    }
    sel.value = String(list.indexOf(keep) >= 0 ? keep : (typeof dflt === "number" ? dflt : RESV_DEFAULT));
    resvWindow = Number(sel.value);
  }

  function initReservoir() {
    buildReservoirWindows(RESV_WINDOWS, RESV_DEFAULT);
    const sel = $("resv-window");
    if (sel) sel.addEventListener("change", () => {
      resvWindow = Number(sel.value);
      renderReservoir(resvLast); // re-render the summary/table for the newly selected window
    });
  }

  function renderReservoir(snap) {
    resvLast = snap || resvLast;
    const summary = $("reservoir-summary");
    const table = $("reservoir-table");
    if (snap && snap.ok === false) {
      if (summary) { summary.textContent = "reservoir ranking unavailable: " + String(snap.error || "unknown"); summary.dataset.tone = "error"; }
      if (table) table.hidden = true;
      return;
    }
    const rankings = cellValue(snap && snap.rankings);
    if (rankings === null || rankings === undefined) {
      // Deferred: an honest "awaiting" state, NOT an empty ranked table.
      if (summary) {
        summary.textContent = "ranking awaiting SRS-RESV-002 (SYS-48 engine) · window " + resvWindow +
          "d — Sharpe / Sortino / momentum not yet computed";
        summary.dataset.tone = "warn";
      }
      if (table) table.hidden = true;
      return;
    }
    // Real ranking (renders when the engine lands): one medallioned row per strategy.
    const rows = Array.isArray(rankings) ? rankings : [];
    const body = $("reservoir-rows");
    if (body) {
      body.textContent = "";
      for (const row of rows) renderReservoirRow(row);
    }
    if (summary) {
      summary.textContent = rows.length + " paper strateg" + (rows.length === 1 ? "y" : "ies") +
        " ranked · window " + resvWindow + "d";
      summary.dataset.tone = "ok";
    }
    if (table) table.hidden = rows.length === 0;
  }

  function renderReservoirRow(row) {
    const body = $("reservoir-rows");
    if (!body) return;
    const rank = cellValue(row.rank);
    const tr = el("tr");
    const rankTd = el("td", "resv-rank");
    const medal = el("span", "resv-medal");
    medal.dataset.rank = rank === null || rank === undefined ? "" : String(rank);
    medal.textContent = rank === null || rank === undefined ? "—" : String(rank);
    rankTd.appendChild(medal);
    tr.appendChild(rankTd);
    const nameTd = el("td", "resv-name"); nameTd.textContent = String(cellValue(row.strategy_id) || "—");
    tr.appendChild(nameTd);
    const sharpeTd = el("td"); metricCellInto(sharpeTd, "ratio", cellValue(row.sharpe != null ? row.sharpe : row.risk_adjusted_score)); tr.appendChild(sharpeTd);
    const sortinoTd = el("td"); metricCellInto(sortinoTd, "ratio", cellValue(row.sortino)); tr.appendChild(sortinoTd);
    // Momentum cell carries the inline-SVG indicator AND the value — built by
    // hand (not metricCellInto, which sets textContent and would wipe the spark).
    const momTd = el("td", "resv-mom");
    const mom = cellValue(row.momentum_score);
    momTd.appendChild(momentumIndicator(mom));
    const momVal = el("span", "resv-momval");
    if (typeof mom === "number" && isFinite(mom)) {
      momVal.textContent = fmt("ratio", mom);
      momVal.className = "resv-momval" + directionClass("ratio", mom);
    } else {
      momVal.textContent = "—"; momVal.className = "resv-momval is-undef";
    }
    momTd.appendChild(momVal);
    tr.appendChild(momTd);
    body.appendChild(tr);
  }

  // A tiny inline-SVG momentum indicator (up/down/flat) — dataviz-style, no deps.
  function momentumIndicator(value) {
    const span = el("span", "resv-spark");
    const dir = typeof value === "number" && isFinite(value) ? (value > 0 ? "up" : value < 0 ? "down" : "flat") : "none";
    span.dataset.dir = dir;
    const path = dir === "up" ? "M1 11 L7 5 L13 8 L19 2"
      : dir === "down" ? "M1 3 L7 8 L13 5 L19 11"
      : dir === "flat" ? "M1 7 H19" : "";
    span.innerHTML = path
      ? '<svg viewBox="0 0 20 14" width="34" height="14" aria-hidden="true"><path d="' + path + '" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>'
      : "";
    return span;
  }

  // ----- UI-1 critical alerts (REST poll; feed deferred to SRS-NOTIF-001) - //
  // The alert vocabulary is the ALERTS channel / GET /api/v1/alerts contract.
  // While the feed cell is deferred the pane renders an honest awaiting state —
  // NEVER "0 active alerts": with detection unwired, "no alerts observed" is
  // not "no alerts occurring".
  // The alerts pane's freshness dot is driven HERE, not by monitorFreshness:
  // while the SRS-NOTIF-001 producer is deferred, a poll-cadence "fresh" would
  // overstate alert-monitoring health (only the placeholder route is healthy).
  function setAlertsDot(state, title) {
    const dot = $("fresh-alerts");
    if (dot) { dot.dataset.state = state; dot.title = title; }
  }

  function renderAlerts(snap) {
    const summary = $("alerts-summary");
    const table = $("alerts-table");
    const beacon = $("alerts-beacon");
    if (snap && snap.ok === false) {
      if (summary) { summary.textContent = "alert feed unavailable: " + String(snap.error || "unknown"); summary.dataset.tone = "error"; }
      if (beacon) beacon.dataset.state = "error";
      if (table) table.hidden = true;
      setAlertsDot("stale", "alert endpoint failing");
      return;
    }
    const feed = cellValue(snap && snap.feed);
    if (feed === null || feed === undefined) {
      if (summary) {
        const owner = shortSource((snap && snap.feed && snap.feed.data_source) || "deferred:SRS-NOTIF-001");
        summary.textContent = "alert feed awaiting " + owner +
          " (operator notifier) — IB connectivity loss and critical failures will surface here";
        summary.dataset.tone = "warn";
      }
      if (beacon) beacon.dataset.state = "deferred";
      if (table) table.hidden = true;
      setAlertsDot("wait", "alert feed deferred — awaiting SRS-NOTIF-001");
      return;
    }
    // Real feed (renders when SRS-NOTIF-001 lands): one row per alert event.
    // A live feed cell whose alert list is missing/malformed must fail closed
    // to the unavailable state — coercing to [] would render a false all-clear.
    if (!Array.isArray(snap.alerts)) {
      renderAlerts({ ok: false, error: "malformed alert feed (alerts is not a list)" });
      return;
    }
    const rows = snap.alerts;
    const body = $("alerts-rows");
    if (body) {
      body.textContent = "";
      for (const alert of rows) renderAlertRow(alert);
    }
    const active = rows.filter((a) => !isAcknowledged(a.acknowledged)).length;
    if (summary) {
      summary.textContent = active + " active critical alert" + (active === 1 ? "" : "s") +
        " · " + rows.length + " recorded";
      summary.dataset.tone = active ? "error" : "ok";
    }
    if (beacon) beacon.dataset.state = active ? "alarm" : "clear";
    if (table) table.hidden = rows.length === 0;
    setAlertsDot("fresh", "alert feed live");
  }

  // FAIL-CLOSED acknowledgement parse: the contract fields are strings, so a
  // truthiness check would read `"false"` as acknowledged and under-count
  // active alerts. Anything but an explicit boolean/string true stays ACTIVE.
  function isAcknowledged(raw) {
    const value = cellValue(raw);
    return value === true || (typeof value === "string" && value.toLowerCase() === "true");
  }

  function renderAlertRow(alert) {
    const body = $("alerts-rows");
    if (!body) return;
    const tr = el("tr");
    const idTd = el("td", "alert-id");
    idTd.textContent = String(cellValue(alert.alert_id) || "—");
    tr.appendChild(idTd);
    const raisedTd = el("td");
    raisedTd.textContent = String(cellValue(alert.raised_at) || "—");
    tr.appendChild(raisedTd);
    const sevTd = el("td");
    const sev = el("span", "alerts__sev");
    sev.dataset.sev = String(cellValue(alert.severity) || "");
    sev.textContent = String(cellValue(alert.severity) || "—");
    sevTd.appendChild(sev);
    tr.appendChild(sevTd);
    const chanTd = el("td");
    chanTd.textContent = String(cellValue(alert.channel) || "—");
    tr.appendChild(chanTd);
    const delivTd = el("td");
    delivTd.textContent = String(cellValue(alert.delivery_status) || "—");
    tr.appendChild(delivTd);
    const ackTd = el("td", "alert-ack");
    ackTd.textContent = isAcknowledged(alert.acknowledged) ? "YES" : "no";
    tr.appendChild(ackTd);
    body.appendChild(tr);
  }

  const ACCOUNT_ROUTE = "/dashboard/api/account";
  const RESERVOIR_ROUTE = "/dashboard/api/reservoir";
  const ALERTS_ROUTE = "/dashboard/api/alerts";

  // First paint + honest "not mounted" fallback for the SRS-UI-003 panels; the
  // WS ACCOUNT_STATUS / RESERVOIR_RANKING events drive live refresh thereafter.
  async function pollAccount() {
    try {
      const res = await fetch(ACCOUNT_ROUTE, { cache: "no-store" });
      if (res.ok) {
        lastChannelAt["ACCOUNT_STATUS"] = performance.now();
        onAccountEvent(await res.json());
      } else if (res.status === 404) {
        renderConnPill(null);
      }
    } catch (_e) { /* transient; next tick retries */ }
    setTimeout(pollAccount, POLL_MS);
  }

  async function pollReservoir() {
    try {
      const res = await fetch(RESERVOIR_ROUTE, { cache: "no-store" });
      if (res.ok) {
        lastChannelAt["RESERVOIR_RANKING"] = performance.now();
        const snap = await res.json();
        buildReservoirWindows(snap.allowed_windows, snap.default_window);
        renderReservoir(snap);
      } else if (res.status === 404) {
        const s = $("reservoir-summary");
        if (s) { s.textContent = "reservoir not mounted — SRS-UI-003 provider not composed on this runtime"; s.dataset.tone = "warn"; }
      }
    } catch (_e) { /* transient; next tick retries */ }
    setTimeout(pollReservoir, POLL_MS);
  }

  // ----- SRS-RES-001 research embed (same-origin /research/ proxy) -------- //

  const RESEARCH_ROUTE = "/dashboard/api/research";
  let researchFrameLoaded = false;

  function renderResearch(snap) {
    const status = $("research-status");
    const open = $("research-open");
    if (!status || !open) return;
    if (snap.configured === false) {
      status.textContent = snap.detail || "research upstream not configured (ATP_RESEARCH_UPSTREAM)";
      status.dataset.tone = "warn";
      open.disabled = true;
      delete open.dataset.embedPath;   // leave nothing stale for a click to find
      return;
    }
    if (snap.upstream_reachable) {
      status.textContent = "research environment reachable (HTTP " + snap.status_code + ") at " + snap.prefix;
      status.dataset.tone = "ok";
      open.disabled = false;
      open.dataset.embedPath = snap.embed_path || "";
    } else {
      // State FIRST, then the probe's reason — symmetric with the reachable
      // branch above, which names its state before its evidence. Delegating the
      // whole line to `detail` left the operator reading a bare socket error
      // ("upstream probe failed: [Errno 61] Connection refused") with the tone
      // colour as the only state signal, and colour alone is not a state.
      status.textContent = snap.detail
        ? "research environment unreachable — " + snap.detail
        : "research environment unreachable";
      status.dataset.tone = "err";
      open.disabled = true;
      delete open.dataset.embedPath;
    }
  }

  // The ONE place an embed is ever opened — the panel button and the SRS-RES-003
  // topbar navigation both funnel through it, so the same-origin check cannot be
  // bypassed by adding a caller (SEC-002 / SYS-43 "no direct service URL").
  function openResearchEmbed(path) {
    const frame = $("research-frame");
    if (!frame || !isSameOriginPath(path)) return false;
    // Lazy same-origin load: the iframe src is only ever the probe-provided
    // /research/… path on THIS origin — never an external URL (SEC-002).
    if (!researchFrameLoaded || frame.getAttribute("src") !== path) {
      frame.src = path;
      researchFrameLoaded = true;
    }
    frame.hidden = false;
    const open = $("research-open");
    if (open) open.textContent = "Reload research environment";
    return true;
  }

  // Every degraded probe branch funnels here. A control that can no longer be
  // backed by a fresh reachability answer must not stay armed on EITHER surface:
  // disarming only the topbar entry would still leave the panel button holding a
  // stale embed path, so one click could open an environment last seen alive
  // several polls ago. Both are cleared together, or neither is honest.
  function disarmResearchControls(reason) {
    const open = $("research-open");
    if (open) {
      open.disabled = true;
      delete open.dataset.embedPath;
    }
    const status = $("research-status");
    if (status) {
      status.textContent = reason;
      status.dataset.tone = "warn";
    }
  }

  function initResearch() {
    const open = $("research-open");
    const frame = $("research-frame");
    if (!open || !frame) return;
    open.addEventListener("click", () => { openResearchEmbed(open.dataset.embedPath); });
  }

  async function pollResearch() {
    try {
      // Bounded like every other poll: a STALLED probe endpoint must reach a
      // degraded branch within one budget, so the SRS-RES-003 navigation
      // control can never stay actionable on a liveness answer that stopped
      // arriving.
      const res = await fetch(RESEARCH_ROUTE, {
        cache: "no-store",
        signal: AbortSignal.timeout(POLL_MS),
      });
      if (res.ok) {
        lastChannelAt["RESEARCH"] = performance.now();
        const snap = await res.json();
        renderResearch(snap);
        setResearchLive(snap);
      } else if (res.status === 404) {
        disarmResearchControls(
          "research not mounted — SRS-RES-001 provider not composed on this runtime"
        );
        setResearchLive(null);
      } else {
        disarmResearchControls(
          "research state unavailable — probe endpoint answered HTTP " + res.status
        );
        setResearchLive(null);
      }
    } catch (_e) {
      // Transient; the next tick retries — but nothing stays armed meanwhile.
      disarmResearchControls(
        "research state unavailable — probe endpoint unreachable or timed out"
      );
      setResearchLive(null);
    }
    renderNav();
    setTimeout(pollResearch, POLL_MS);
  }

  // ----- SRS-RES-003 primary research navigation (SyRS SYS-43) ----------- //
  //
  // "The operator can open the embedded Jupyter environment from the primary
  // dashboard workflow without using a direct service URL."
  //
  // The control gates on TWO independent facts, and neither may stand in for
  // the other:
  //   routable  — composition: is the same-origin /research/ prefix registered
  //               on THIS runtime? (GET /dashboard/api/navigation, probe-free)
  //   reachable — liveness: is the upstream answering right now?
  //               (GET /dashboard/api/research, the SRS-RES-001 probe)
  // Both must be present AND fresh. Every degraded branch — route 404, HTTP
  // error, stalled fetch, malformed body, a probe answer that aged out — clears
  // its own side and disarms the control, because a navigation affordance left
  // actionable on stale truth is a false promise, not a convenience.

  const NAVIGATION_ROUTE = "/dashboard/api/navigation";
  const RESEARCH_LIVE_STALE_MS = POLL_MS * 3;   // liveness budget for the probe

  let navEntry = null;          // fail-closed projection of the nav model
  // The caption is width-bounded, so the short form is what the operator reads
  // at a glance and the long form is the title — neither is allowed to be more
  // optimistic than the other.
  let navReason = { short: "checking…", detail: "checking navigation model…" };
  let researchLive = null;      // {reachable, detail} from the RES-001 probe
  let researchLiveAt = 0;
  let deepLinkPending = false;
  let deepLinkDeadline = 0;

  // Mirrors atp_dashboard/navigation.py::same_origin_target — a target must be
  // a root-relative path on THIS origin. Defence in depth: the server already
  // refuses anything else, and the browser refuses it again before it can
  // become an iframe src.
  function isSameOriginPath(value) {
    if (typeof value !== "string" || value === "") return false;
    if (!value.startsWith("/") || value.startsWith("//")) return false;
    if (/[\s\u0000-\u0020\u007f:\\@]/.test(value)) return false;
    return value.split("/").indexOf("..") === -1;
  }

  // Guard the shape BEFORE reading fields — a malformed feed must fail closed,
  // never throw its way past the disarm.
  function plainObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  }

  function researchNavEntry(snap) {
    const body = plainObject(snap);
    if (!body || !Array.isArray(body.entries)) return null;
    for (const raw of body.entries) {
      const entry = plainObject(raw);
      if (!entry || entry.id !== "research") continue;
      // Strict === true: a truthy string must never arm the control.
      const routable = entry.routable === true && isSameOriginPath(entry.target);
      return {
        routable: routable,
        target: routable ? entry.target : null,
        detail: typeof entry.detail === "string" ? entry.detail : "",
      };
    }
    return null;
  }

  function setResearchLive(snap) {
    const body = plainObject(snap);
    if (!body || body.configured !== true) {
      researchLive = null;
      researchLiveAt = 0;
      return;
    }
    researchLive = {
      reachable: body.upstream_reachable === true,
      detail: typeof body.detail === "string" ? body.detail : "",
      embedPath: isSameOriginPath(body.embed_path) ? body.embed_path : null,
    };
    researchLiveAt = performance.now();
  }

  function researchLiveFresh() {
    return researchLive !== null && researchLiveAt > 0
      && (performance.now() - researchLiveAt) <= RESEARCH_LIVE_STALE_MS;
  }

  function revealResearchPanel() {
    const panel = document.querySelector(".panel--research");
    if (!panel) return;
    const reduced = window.matchMedia
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    panel.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
    panel.dataset.navFocus = "true";
    setTimeout(() => { delete panel.dataset.navFocus; }, 2000);
  }

  // Single render fn: every state the control can show is computed here from
  // the two facts, so no code path can leave a tone/label/target out of sync.
  function renderNav() {
    const link = $("nav-research");
    const caption = $("nav-research-state");
    if (!link || !caption) return;

    let state = "deferred";
    let short = navReason;
    let detail = navReason;
    let embedPath = null;

    if (navEntry === null) {
      state = "deferred";
      short = navReason.short;
      detail = navReason.detail;
    } else if (!navEntry.routable) {
      state = "deferred";
      short = "not configured";
      detail = navEntry.detail || "research environment not configured";
    } else if (!researchLiveFresh()) {
      state = "degraded";
      short = "probe state stale";
      detail = "research reachability unknown — the " + RESEARCH_ROUTE
        + " probe has not reported within its budget";
    } else if (!researchLive.reachable) {
      state = "down";
      short = "upstream unreachable";
      detail = researchLive.detail || "research upstream unreachable";
    } else {
      // The ONLY armed state: routable, fresh, and reachable. The path opened
      // is the probe's same-origin embed_path when present, else the prefix.
      state = "ready";
      short = "open embedded Jupyter";
      embedPath = researchLive.embedPath || navEntry.target;
      detail = "open the embedded Jupyter environment at " + embedPath
        + " (same-origin — no direct service URL)";
    }

    link.dataset.state = state;
    caption.textContent = short;
    link.title = detail;
    if (state === "ready" && isSameOriginPath(embedPath)) {
      link.dataset.embedPath = embedPath;
    } else {
      delete link.dataset.embedPath;   // nothing stale left for a click to find
    }
    maybeConsumeDeepLink(link);
  }

  // /dashboard#research — addressable navigation. The intent is ONE-SHOT and
  // bounded: it opens only off a completed poll that proves the environment
  // reachable, and is dropped once the budget passes. It never acts on assumed
  // state, and it never retries forever.
  function armDeepLink() {
    if (window.location.hash !== "#research") return;
    deepLinkPending = true;
    deepLinkDeadline = performance.now() + POLL_MS * 3;
    revealResearchPanel();
  }

  function maybeConsumeDeepLink(link) {
    if (!deepLinkPending) return;
    if (link.dataset.state === "ready") {
      deepLinkPending = false;
      openResearchEmbed(link.dataset.embedPath);
    } else if (performance.now() > deepLinkDeadline) {
      deepLinkPending = false;   // gives up honestly rather than opening blind
    }
  }

  function initNavigation() {
    const link = $("nav-research");
    if (!link) return;
    link.addEventListener("click", (event) => {
      // The href is this origin's own #research anchor (it already works with
      // JS off). We take over to ALSO open the embed — but only when the
      // control is genuinely armed. When it is not, the operator still lands on
      // the panel, which carries the probe-derived reason: navigation always
      // tells the truth, and never fabricates an open.
      event.preventDefault();
      if (window.location.hash !== "#research") {
        window.history.replaceState(null, "", "#research");
      }
      revealResearchPanel();
      if (link.dataset.state === "ready") openResearchEmbed(link.dataset.embedPath);
    });
    window.addEventListener("hashchange", armDeepLink);
    armDeepLink();
  }

  async function pollNavigation() {
    try {
      const res = await fetch(NAVIGATION_ROUTE, {
        cache: "no-store",
        signal: AbortSignal.timeout(POLL_MS),
      });
      if (res.ok) {
        navEntry = researchNavEntry(await res.json());
        if (navEntry === null) {
          navReason = {
            short: "model unreadable",
            detail: "navigation model unreadable — malformed response",
          };
        }
      } else if (res.status === 404) {
        navEntry = null;
        navReason = {
          short: "not mounted",
          detail: "not mounted — SRS-RES-001 embed not composed on this runtime",
        };
      } else {
        navEntry = null;
        navReason = {
          short: "route HTTP " + res.status,
          detail: "navigation route answered HTTP " + res.status,
        };
      }
    } catch (_e) {
      navEntry = null;
      navReason = {
        short: "route unreachable",
        detail: "navigation route unreachable or timed out",
      };
    }
    renderNav();
    setTimeout(pollNavigation, POLL_MS);
  }

  async function pollAlerts() {
    try {
      // Bounded fetch: a STALLED endpoint (never resolving) must not leave the
      // previous safety state on screen indefinitely — abort to the explicit
      // unavailable branch within one poll budget.
      const res = await fetch(ALERTS_ROUTE, {
        cache: "no-store",
        signal: AbortSignal.timeout(POLL_MS),
      });
      if (res.ok) {
        renderAlerts(await res.json());
      } else if (res.status === 404) {
        // Route disappearance fails closed like every other degraded branch:
        // stale rows / a stale clear-or-alarm beacon must never outlive the
        // provider that produced them.
        const s = $("alerts-summary");
        if (s) { s.textContent = "alerts pane not mounted — UI-1 provider not composed on this runtime"; s.dataset.tone = "warn"; }
        const table = $("alerts-table");
        if (table) table.hidden = true;
        const rows = $("alerts-rows");
        if (rows) rows.textContent = "";
        const beacon = $("alerts-beacon");
        if (beacon) beacon.dataset.state = "deferred";
        setAlertsDot("wait", "alerts route not mounted");
      } else {
        // A failing endpoint must never leave a stale "clear"/alert-count on a
        // safety-critical pane — render the explicit unavailable state.
        renderAlerts({ ok: false, error: "HTTP " + res.status });
      }
    } catch (_e) {
      renderAlerts({ ok: false, error: "endpoint unreachable" });
    }
    setTimeout(pollAlerts, POLL_MS);
  }

  function setConn(state, label) {
    const c = $("conn"); c.dataset.state = state; $("conn-label").textContent = label;
  }

  // ----- system snapshot poll (health + latency) ------------------------- //
  async function poll() {
    try {
      const res = await fetch("/dashboard/api/system", { cache: "no-store" });
      if (res.ok) applySnapshot(await res.json());
    } catch (_e) { /* transient; next tick retries */ }
    setTimeout(poll, POLL_MS);
  }

  // ----- strategy-inventory poll (SRS-UI-002 first paint + fallback) ------ //
  // The WS STRATEGY_STATE events drive live updates; this poll gives the table
  // its first paint and reports an UN-mounted inventory honestly (the route is
  // registered only when a composer mounts the SRS-UI-002 provider). EVERY
  // degraded branch fails closed by clearing the rows (which disarms any
  // staged PROMOTE LIVE): the WS transport shares this same server, so a sick
  // or unreachable endpoint means no transport is authoritative — stale rows
  // must not stay actionable, and a malformed snapshot is unknown truth, never
  // "no strategies deployed".
  function inventoryUnavailable(reason) {
    clearInventoryRows();
    closeInventorySources();
    const summary = $("inventory-summary");
    if (summary) {
      summary.textContent = reason;
      summary.dataset.tone = "error";
    }
  }

  async function pollStrategies() {
    try {
      const res = await fetch("/dashboard/api/strategies", {
        cache: "no-store",
        signal: AbortSignal.timeout(POLL_MS),
      });
      if (res.ok) {
        const snap = await res.json();
        if (snap.ok === true && Array.isArray(snap.strategies)) {
          onInventoryEvent({
            event: "inventory-summary",
            ok: true,
            strategy_count: snap.strategies.length,
            // Thread the server's capability report through this synthesized
            // summary — omitting it would silently fail the rollback control
            // closed on every REST tick even where the route IS served.
            rollback_available: snap.rollback_available,
          }, "rest");
          for (const row of snap.strategies) renderInventoryRow(row, "rest");
        } else if (snap.ok === false) {
          onInventoryEvent({ event: "inventory-summary", ok: false, error: snap.error }, "rest");
        } else {
          inventoryUnavailable("inventory unavailable: malformed snapshot");
        }
      } else if (res.status === 404) {
        // Route disappearance: the provider is no longer composed on this
        // runtime — the caption alone is not enough, the rows go too.
        clearInventoryRows();
        closeInventorySources();
        const summary = $("inventory-summary");
        if (summary) {
          summary.textContent = "inventory not mounted — SRS-UI-002 provider not composed on this runtime";
          summary.dataset.tone = "warn";
        }
      } else {
        inventoryUnavailable("inventory unavailable: endpoint " + res.status);
      }
    } catch (_e) {
      inventoryUnavailable("inventory unavailable: endpoint unreachable");
    }
    setTimeout(pollStrategies, POLL_MS);
  }

  function applySnapshot(snap) {
    // The system snapshot poll is the "SYSTEM" (latency panel) freshness source.
    lastChannelAt["SYSTEM"] = performance.now();
    const health = snap.health || {};
    renderReadiness(health);
    const lat = snap.latency || {};
    setField("body-latency", "order_signal_to_ack_p95_ms", lat.order_signal_to_ack_p95_ms, "ms");
    setField("body-latency", "pipeline_fanout_p95_ms", lat.pipeline_fanout_p95_ms, "ms");
    if (typeof lat.refresh_budget_ms === "number") {
      setField("body-latency", "refresh_budget_ms", { value: lat.refresh_budget_ms, data_source: "live" }, "ms");
    }
  }

  function renderReadiness(health) {
    const ok = health.ok === true;
    const errs = Array.isArray(health.errors) ? health.errors : [];
    const warns = Array.isArray(health.warnings) ? health.warnings : [];
    const chip = $("readiness");
    chip.dataset.ok = ok ? "true" : errs.length ? "bad" : "false";
    $("readiness-state").textContent = String(health.state || "—");

    const pill = $("health-pill");
    pill.textContent = String(health.state || "—");
    pill.className = "pill " + (ok ? "pill--ok" : errs.length ? "pill--bad" : "pill--warn");

    const notes = $("health-notes");
    notes.textContent = "";
    const items = errs.map((e) => ["err", e]).concat(warns.map((w) => ["warn", w]));
    if (!items.length && ok) items.push(["ok", "all readiness checks nominal"]);
    for (const [, note] of items.slice(0, 4)) {
      const li = document.createElement("li"); li.textContent = noteText(note); notes.appendChild(li);
    }
  }

  // A readiness finding is a structured record ({key, reason, ...}) — render it
  // operator-readable (ERR-9: the failure must be inspectable from the
  // dashboard), never String(object) ("[object Object]").
  function noteText(note) {
    if (note && typeof note === "object") {
      const key = note.key || note.category || "";
      const reason = note.reason || note.message || "";
      if (key && reason) return key + " — " + reason;
      if (key || reason) return String(key || reason);
      return JSON.stringify(note);
    }
    return String(note);
  }

  // ----- UI-4 kill switch: control + Liquidate-Sequence status feedback -- //
  // CONTROL: two-step arm-then-fire against the CONTRACT route on this same
  // runtime (never a /dashboard path). One in-flight request at a time, one
  // staged confirmation at a time, and BOTH triggers (the topbar affordance and
  // the panel's) drive this single state machine — a mutation that liquidates
  // every live position must not be racing itself.
  //
  // FEEDBACK: the panel renders the six sequence legs the AC names
  // (cancellation, liquidation submission, timeout, notification, disconnect —
  // plus the paper-engine halt the sequence starts with) from the READ route
  // /dashboard/api/kill-switch, which reads the durable last-activation record
  // and the SRS-LOG-001 SYS-44b timeout record.
  //
  // FAIL CLOSED, everywhere. A leg renders as resolved ONLY when the payload
  // carries a live value that agrees with its status; every other case — a
  // deferred cell, a missing/unknown status, a malformed payload, a non-OK
  // response, a 404, an unreachable or stalled endpoint — renders UNKNOWN and
  // clears the receipt. A dashboard that cannot observe the liquidate sequence
  // must SAY so: a stale or invented "IB DISCONNECTED" is a lie about whether
  // the position is still live.
  const KILL_SWITCH_ROUTE = "/api/v1/kill-switch?confirm=true";
  const KILL_SWITCH_STATUS_ROUTE = "/dashboard/api/kill-switch";
  const KILL_ARM_WINDOW_MS = 5000;
  const KILL_FETCH_TIMEOUT_MS = 20000;
  const KILL_RESTING_CAPTION = "two-step confirmation required";
  // The rail the pane shows before (and instead of) any observed status. Order
  // and labels mirror atp_dashboard.killswitch.KILL_SWITCH_SEQUENCE; a payload
  // that disagrees is drift and is rejected wholesale.
  const KS_PHASES = [
    ["halt", "PAPER ENGINES HALTED", false],
    ["cancellation", "CANCELLATION", false],
    ["liquidation", "LIQUIDATION SUBMISSION", false],
    ["timeout", "UNFILLED TIMEOUT", true],
    ["notification", "OPERATOR NOTIFICATION", true],
    ["disconnect", "IB DISCONNECT", false],
  ];
  let killArmTimer = null;
  let killInFlight = false;
  // The activation id a 2xx designated. The status pane must agree with it;
  // a snapshot naming a DIFFERENT activation is surfaced, never silently shown.
  let killConfirmedId = null;

  function killStatus(text, tone) {
    // Both readouts carry the same words — the topbar chip and the panel are
    // two views of one control, never two stories.
    const top = $("killswitch-status");
    if (top) { top.textContent = text; top.dataset.tone = tone || ""; }
    const panel = $("ks-status");
    if (panel) { panel.textContent = text; panel.dataset.tone = tone || ""; }
  }

  function ksState(state) {
    const root = $("ks");
    if (root) root.dataset.state = state;
  }

  function killButtons() {
    return [$("killswitch-btn"), $("ks-btn")].filter(Boolean);
  }

  function disarmKillSwitch(restoreResting) {
    if (killArmTimer) { clearTimeout(killArmTimer); killArmTimer = null; }
    const top = $("killswitch-btn");
    if (top) { top.dataset.armed = "false"; top.textContent = "KILL SWITCH"; }
    const panel = $("ks-btn");
    if (panel) { panel.dataset.armed = "false"; panel.textContent = "ARM KILL SWITCH"; }
    ksState(killConfirmedId ? "fired" : "resting");
    // A leftover "armed — confirm within 5s" caption with nothing staged is
    // stale state: restore the RESTING caption, never leave the operator
    // believing a confirmation is still pending.
    if (restoreResting) killStatus(KILL_RESTING_CAPTION, "");
  }

  function armKillSwitch() {
    const top = $("killswitch-btn");
    if (top) { top.dataset.armed = "true"; top.textContent = "CONFIRM LIQUIDATE?"; }
    const panel = $("ks-btn");
    if (panel) { panel.dataset.armed = "true"; panel.textContent = "CONFIRM LIQUIDATE"; }
    ksState("armed");
    killStatus("ARMED — confirm within 5s to cancel, liquidate and disconnect", "armed");
    if (killArmTimer) clearTimeout(killArmTimer);
    killArmTimer = setTimeout(() => disarmKillSwitch(true), KILL_ARM_WINDOW_MS);
  }

  function killArmed() {
    const panel = $("ks-btn");
    if (panel) return panel.dataset.armed === "true";
    const top = $("killswitch-btn");
    return !!top && top.dataset.armed === "true";
  }

  async function fireKillSwitch() {
    if (killInFlight) return;
    killInFlight = true;
    if (killArmTimer) { clearTimeout(killArmTimer); killArmTimer = null; }
    const buttons = killButtons();
    // The staged confirmation is CONSUMED the moment it fires: clear the armed
    // state immediately (a control still reading "CONFIRM LIQUIDATE" while its
    // request is in flight is stale state), and disable every trigger until the
    // request settles.
    buttons.forEach((b) => { b.disabled = true; b.dataset.armed = "false"; });
    const topFiring = $("killswitch-btn");
    if (topFiring) topFiring.textContent = "ACTIVATING…";
    const panelFiring = $("ks-btn");
    if (panelFiring) panelFiring.textContent = "ACTIVATING…";
    ksState("firing");
    killStatus("activating…", "pending");
    try {
      const res = await fetch(KILL_SWITCH_ROUTE, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
        // A STALLED runtime must not leave the control wedged "activating…"
        // forever with every sibling trigger inert.
        signal: AbortSignal.timeout(KILL_FETCH_TIMEOUT_MS),
      });
      const body = await res.json().catch(() => null);
      if (res.ok) {
        // Identity binding: a 2xx designates an activation only when the
        // runtime echoes a concrete activation id. A success-shaped body
        // without one proves nothing about what ran.
        const id = body && typeof body.activation_id === "string" ? body.activation_id.trim() : "";
        if (!id) {
          killStatus("REFUSED: activation response carries no activation_id — " +
            "cannot confirm what ran; inspect kill-switch status", "error");
        } else {
          killConfirmedId = id;
          // A 200 means the sequence RAN — not that every phase succeeded.
          // Count FAILED per-order outcomes so a partial failure is loudly
          // distinguishable from a clean liquidation.
          const countFailed = (entries) => Array.isArray(entries)
            ? entries.filter((e) => e && e.outcome && e.outcome.status === "FAILED").length
            : 0;
          const failed = countFailed(body.liquidation_orders) + countFailed(body.cancelled_orders);
          const disconnected = body.ib_gateway_disconnected === true;
          const summary =
            "activated " + id +
            ": engines_halted=" + String(body.paper_engines_halted) +
            " liquidations=" + (Array.isArray(body.liquidation_orders) ? body.liquidation_orders.length : "?") +
            " cancels=" + (Array.isArray(body.cancelled_orders) ? body.cancelled_orders.length : "?") +
            " ib_disconnected=" + String(body.ib_gateway_disconnected);
          if (failed > 0 || !disconnected) {
            killStatus(summary + " — WITH FAILURES: " + failed + " order phase(s) FAILED" +
              (!disconnected ? ", IB NOT disconnected" : "") + " — inspect the sequence below", "error");
          } else {
            killStatus(summary, "fired");
          }
        }
      } else {
        const err = body && body.error ? body.error : {};
        const owner = err.detail && err.detail.owner ? " · owner " + String(err.detail.owner) : "";
        killStatus("REFUSED " + res.status + " " + String(err.type || err.category || "UNKNOWN") + owner, "error");
      }
    } catch (error) {
      killStatus("FAILED: " + String(error), "error");
    }
    buttons.forEach((b) => { b.disabled = false; });
    killInFlight = false;
    disarmKillSwitch(false);
    // Re-read the durable status immediately: the receipt below must reflect
    // what actually landed, not what the POST said.
    pollKillSwitchOnce();
  }

  function onKillTrigger() {
    // The in-flight guard comes FIRST: while one activation is settling, every
    // sibling trigger is inert (no second arm, no second POST).
    if (killInFlight) return;
    if (!killArmed()) { armKillSwitch(); return; }
    fireKillSwitch();
  }

  // ----- UI-4 status pane rendering (fail-closed) ------------------------ //

  function ksRung(phase, label, branch, status, detail, owner) {
    const li = document.createElement("li");
    li.className = "ks__rung";
    li.dataset.phase = phase;
    li.dataset.branch = branch ? "true" : "false";
    li.dataset.status = status;
    const node = document.createElement("span");
    node.className = "ks__node";
    node.setAttribute("aria-hidden", "true");
    const bodyEl = document.createElement("div");
    bodyEl.className = "ks__body";
    const row = document.createElement("div");
    row.className = "ks__labelrow";
    const ord = document.createElement("span");
    ord.className = "ks__ord";
    ord.textContent = String(KS_PHASES.findIndex((p) => p[0] === phase) + 1).padStart(2, "0");
    const name = document.createElement("span");
    name.className = "ks__label";
    name.textContent = label;
    const badge = document.createElement("span");
    badge.className = "ks__badge";
    badge.dataset.status = status;
    badge.textContent = status;
    row.appendChild(ord); row.appendChild(name); row.appendChild(badge);
    const det = document.createElement("span");
    det.className = "ks__detail";
    det.textContent = detail;
    bodyEl.appendChild(row); bodyEl.appendChild(det);
    if (owner) {
      const own = document.createElement("span");
      own.className = "ks__owner";
      own.textContent = "awaiting " + owner;
      bodyEl.appendChild(own);
    }
    li.appendChild(node); li.appendChild(bodyEl);
    return li;
  }

  function ksReceipt(cells) {
    const set = (id, text, tone) => {
      const el = $(id);
      if (!el) return;
      el.textContent = text;
      el.dataset.tone = tone || "";
    };
    set("ks-activation-id", cells.id, cells.idTone);
    set("ks-activated-at", cells.at, "");
    set("ks-ran-clean", cells.ranClean, cells.ranCleanTone);
    set("ks-nfr", cells.nfr, cells.nfrTone);
    set("ks-halt-latency", cells.latency, cells.latencyTone);
    set("ks-audit", cells.audit, cells.auditTone);
  }

  // The single fail-closed clear: every leg UNKNOWN, receipt blank, orders
  // hidden, tier unknown. Used by EVERY degraded branch — a partial clear that
  // leaves one green rung on screen is exactly the bug this guards against.
  function ksUnknown(reason, tone) {
    const rail = $("ks-rail");
    if (rail) {
      rail.textContent = "";
      for (const [phase, label, branch] of KS_PHASES) {
        rail.appendChild(ksRung(phase, label, branch, "UNKNOWN", "not observed", null));
      }
    }
    ksReceipt({
      // The identity reads UNKNOWN, not "—": a blank dash invites "nothing
      // happened", which is exactly the reading this clear exists to prevent.
      id: "UNKNOWN", idTone: "unknown", at: "—",
      ranClean: "UNKNOWN", ranCleanTone: "unknown",
      nfr: "UNKNOWN", nfrTone: "unknown",
      latency: "UNKNOWN", latencyTone: "unknown",
      audit: "UNKNOWN", auditTone: "unknown",
    });
    const table = $("ks-orders-table");
    if (table) table.hidden = true;
    const rows = $("ks-orders");
    if (rows) rows.textContent = "";
    const tier = $("ks-tier");
    if (tier) { tier.dataset.tier = "unknown"; tier.textContent = "TIER UNKNOWN"; }
    const note = $("ks-note");
    if (note) { note.textContent = reason; note.dataset.tone = tone || "warn"; }
    if (!killInFlight) ksState(killConfirmedId ? "fired" : "resting");
  }

  function ksTriBool(value, okText, badText) {
    if (value === true) return [okText, "ok"];
    if (value === false) return [badText, "bad"];
    return ["UNKNOWN", "unknown"];
  }

  function renderKillSwitch(snap) {
    if (!snap || typeof snap !== "object") {
      ksUnknown("kill-switch status payload malformed — treating every leg as UNKNOWN", "error");
      return;
    }
    const seq = snap.sequence;
    // Schema drift is not a rendering problem, it is an honesty problem: if the
    // rail does not match the phase order this client knows, refuse the whole
    // payload rather than render a partial sequence.
    const shaped = Array.isArray(seq) && seq.length === KS_PHASES.length &&
      seq.every((leg, i) => leg && typeof leg === "object" &&
        leg.phase === KS_PHASES[i][0] && typeof leg.status === "string");
    if (!shaped) {
      ksUnknown("kill-switch status payload does not match the known sequence contract — " +
        "treating every leg as UNKNOWN", "error");
      return;
    }
    const rail = $("ks-rail");
    if (rail) {
      rail.textContent = "";
      seq.forEach((leg, i) => {
        // A rung is resolved ONLY when the payload carries a live value that
        // AGREES with the status. A deferred cell (value null) or any
        // disagreement renders UNKNOWN — the server cannot talk this client
        // into drawing a green leg it did not substantiate.
        const resolved = typeof leg.value === "string" && leg.value === leg.status &&
          leg.status !== "UNKNOWN";
        const status = resolved ? leg.status : "UNKNOWN";
        const detail = typeof leg.detail === "string" && leg.detail ? leg.detail : "not observed";
        const owner = resolved ? null : (typeof leg.owner === "string" ? leg.owner : null);
        rail.appendChild(ksRung(KS_PHASES[i][0], KS_PHASES[i][1], KS_PHASES[i][2], status, detail, owner));
      });
    }

    const activated = snap.activated === true ? true : (snap.activated === false ? false : null);
    const id = typeof snap.activation_id === "string" && snap.activation_id ? snap.activation_id : null;
    const [ranClean, ranCleanTone] = ksTriBool(snap.ran_clean, "CLEAN", "WITH FAILURES");
    const [nfr, nfrTone] = ksTriBool(snap.within_nfr_p3, "WITHIN 5s", "BREACHED");
    const [audit, auditTone] = ksTriBool(snap.audit_recorded, "RECORDED", "NOT RECORDED");
    const latencyMs = typeof snap.halted_log_latency_ms === "number" && isFinite(snap.halted_log_latency_ms)
      ? snap.halted_log_latency_ms : null;
    const budget = typeof snap.halt_observability_budget_ms === "number"
      ? snap.halt_observability_budget_ms : 1000;
    ksReceipt({
      id: id || (activated === false ? "no activation recorded" : "UNKNOWN"),
      idTone: id ? "" : (activated === false ? "" : "unknown"),
      at: typeof snap.activated_at === "string" && snap.activated_at ? snap.activated_at : "—",
      ranClean: ranClean, ranCleanTone: ranCleanTone,
      nfr: nfr, nfrTone: nfrTone,
      latency: latencyMs === null ? "UNKNOWN" : Math.round(latencyMs) + " ms / " + budget + " ms",
      latencyTone: latencyMs === null ? "unknown" : (latencyMs <= budget ? "ok" : "bad"),
      audit: audit, auditTone: auditTone,
    });

    // Orders: null means UNKNOWN, and an unknown order set must not render as
    // an empty (all-clear-shaped) table.
    const rows = $("ks-orders");
    const table = $("ks-orders-table");
    const orders = Array.isArray(snap.orders) ? snap.orders : null;
    if (rows) rows.textContent = "";
    if (table) table.hidden = !orders || !orders.length;
    if (rows && orders) {
      for (const order of orders) {
        if (!order || typeof order !== "object") continue;
        const tr = document.createElement("tr");
        const cells = [
          [String(order.kind || "—"), "kind"],
          [String(order.symbol || "—"), ""],
          [String(order.broker_order_id || order.order_id || "—"), ""],
          [order.quantity === null || order.quantity === undefined ? "—" : String(order.quantity), ""],
          [String(order.status || "UNKNOWN"), "status"],
        ];
        for (const [text, kind] of cells) {
          const td = document.createElement("td");
          if (kind === "kind") {
            const span = document.createElement("span");
            span.className = "ks__kind";
            span.textContent = text;
            td.appendChild(span);
          } else {
            td.textContent = text;
            if (kind === "status") td.dataset.status = text;
          }
          tr.appendChild(td);
        }
        rows.appendChild(tr);
      }
    }

    // Transport tier: only the two declared tiers are ever shown as such.
    const tier = $("ks-tier");
    if (tier) {
      const t = snap.tier === "FIXTURE" || snap.tier === "LIVE" ? snap.tier : null;
      tier.dataset.tier = t || "unknown";
      tier.textContent = t === "FIXTURE" ? "FIXTURE DRILL" : (t === "LIVE" ? "LIVE" : "TIER UNKNOWN");
    }

    const note = $("ks-note");
    if (note) {
      const errors = Array.isArray(snap.errors) ? snap.errors.filter((e) => typeof e === "string") : [];
      if (killConfirmedId && id && id !== killConfirmedId) {
        // The pane is describing a DIFFERENT activation than the one this
        // client confirmed. Name both ids rather than let the operator read
        // someone else's receipt as their own.
        note.textContent = "MISMATCH: this browser confirmed " + killConfirmedId +
          " but the recorded activation is " + id + " — verify before acting";
        note.dataset.tone = "error";
      } else if (errors.length) {
        note.textContent = errors.join(" · ");
        note.dataset.tone = "error";
      } else if (activated === null) {
        note.textContent = "activation status UNKNOWN — the dashboard cannot observe this kill switch";
        note.dataset.tone = "warn";
      } else if (activated === false) {
        note.textContent = "no activation recorded in the configured state directory";
        note.dataset.tone = "";
      } else {
        note.textContent = "sequence read from the durable activation record" +
          (snap.timeout_correlated === false
            ? " · SYS-44b legs show the latest timeout record, which carries no key linking it to this activation (SRS-SAFE-002)"
            : "");
        note.dataset.tone = "";
      }
    }
    if (!killInFlight) ksState(activated === true ? "fired" : (killConfirmedId ? "fired" : "resting"));
  }

  async function pollKillSwitchOnce() {
    try {
      // Bounded: a stalled endpoint must not leave the previous safety state on
      // screen indefinitely.
      const res = await fetch(KILL_SWITCH_STATUS_ROUTE, {
        cache: "no-store",
        signal: AbortSignal.timeout(POLL_MS),
      });
      if (res.ok) {
        let body = null;
        try { body = await res.json(); } catch (_e) { body = null; }
        renderKillSwitch(body);
      } else if (res.status === 404) {
        ksUnknown("kill-switch status not mounted — UI-4 provider not composed on this runtime", "warn");
      } else {
        ksUnknown("kill-switch status unavailable (HTTP " + res.status + ") — every leg UNKNOWN", "error");
      }
    } catch (_e) {
      ksUnknown("kill-switch status endpoint unreachable — every leg UNKNOWN", "error");
    }
  }

  async function pollKillSwitch() {
    await pollKillSwitchOnce();
    setTimeout(pollKillSwitch, POLL_MS);
  }

  function initKillSwitch() {
    // Paint the fail-closed rail before the first poll resolves: an empty pane
    // must never be the first thing an operator reads as "nothing wrong".
    ksUnknown("awaiting kill-switch status…", "warn");
    killStatus(KILL_RESTING_CAPTION, "");
    for (const btn of killButtons()) btn.addEventListener("click", onKillTrigger);
  }

  // ----- UI-3 backtest controls + result history (SRS-UI-004 / SYS-42/43a) //
  // CONTROLS: the launch form POSTs to the CONTRACT route on this same runtime
  // (never a /dashboard path) and renders the runtime's own response verbatim —
  // a 501 HANDLER_DEFERRED (the live launch handler is SRS-API-001's) is shown
  // as an honest "deferred", never dressed as a success. HISTORY: the REAL
  // SRS-BT-009 store via GET /dashboard/api/backtests; a row drills into an
  // inline equity-curve chart + trade log + SPY benchmark comparison.
  const BACKTEST_LAUNCH_ROUTE = "/api/v1/backtests";
  const BACKTEST_HISTORY_ROUTE = "/dashboard/api/backtests";
  const btRecords = Object.create(null); // run_id -> record (for drill-down)
  let btSelected = null;

  function fmtMinor(minor) {
    const n = Number(minor);
    if (!isFinite(n)) return "—";
    return (n / 100).toLocaleString(undefined, { style: "currency", currency: "USD" });
  }
  function fmtAxisMinor(minor) {
    return (Number(minor) / 100).toLocaleString(undefined, {
      style: "currency", currency: "USD", maximumFractionDigits: 0,
    });
  }
  function paramsText(record) {
    const ps = Array.isArray(record.parameters) ? record.parameters : [];
    return ps.length ? ps.map((p) => p.key + "=" + p.value).join(", ") : "—";
  }
  // A metric cell: null => mathematically undefined (real run, no fabricated 0).
  function metricCellInto(td, kind, value) {
    if (value === null || value === undefined) {
      td.textContent = "—"; td.className = "bt-num is-undef"; return;
    }
    td.textContent = fmt(kind, value);
    td.className = "bt-num" + directionClass(kind, value);
  }

  function initBacktest() {
    const form = $("backtest-form");
    if (!form) return;
    if (!$("bt-start").value) $("bt-start").value = "2024-01-01";
    if (!$("bt-end").value) $("bt-end").value = "2024-12-31";
    form.addEventListener("submit", submitBacktest);
  }

  async function submitBacktest(ev) {
    ev.preventDefault();
    const strategy = $("bt-strategy").value.trim();
    const start = $("bt-start").value;
    const end = $("bt-end").value;
    if (!strategy || !start || !end) {
      btRunStatus("strategy, start and end are required", "error");
      return;
    }
    const body = {
      strategy_id: strategy,
      start_date: start,
      end_date: end,
      parameter_overrides: $("bt-params").value.trim(),
      cost_model: $("bt-cost").value,
    };
    const btn = $("bt-run");
    btn.disabled = true;
    btRunStatus("submitting…", "pending");
    try {
      const res = await fetch(BACKTEST_LAUNCH_ROUTE, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      let payload = {};
      try { payload = await res.json(); } catch (_e) { /* empty body */ }
      if (res.ok) {
        // A live launch (SRS-API-001) would return {backtest_id, queued_at}.
        btRunStatus(
          "queued " + String(payload.backtest_id || "") +
          (payload.queued_at ? " @ " + String(payload.queued_at) : ""), "ok"
        );
      } else if (res.status === 501) {
        // The runtime names the deferred owner itself; show it verbatim rather
        // than guessing (declared owner SRS-BT-001; REST handler wiring SRS-API-001).
        const owner = ((payload.error || {}).detail || {}).owner || "the backtest launch owner";
        btRunStatus("launch handler not yet wired — deferred to " + String(owner), "deferred");
      } else {
        const err = payload && payload.error ? payload.error : {};
        btRunStatus("REFUSED " + res.status + " " + String(err.type || "UNKNOWN"), "error");
      }
    } catch (error) {
      btRunStatus("FAILED: " + String(error), "error");
    }
    btn.disabled = false;
  }

  function btRunStatus(text, tone) {
    const el = $("bt-run-status");
    if (!el) return;
    el.textContent = text; el.dataset.tone = tone;
  }

  async function pollBacktests() {
    try {
      const res = await fetch(BACKTEST_HISTORY_ROUTE, { cache: "no-store" });
      if (res.ok) {
        lastChannelAt["BACKTEST"] = performance.now();
        renderBacktestHistory(await res.json());
      } else if (res.status === 404) {
        const s = $("backtest-summary");
        if (s) {
          s.textContent = "history not mounted — SRS-UI-004 provider not composed on this runtime";
          s.dataset.tone = "warn";
        }
      }
    } catch (_e) { /* transient; next tick retries */ }
    setTimeout(pollBacktests, POLL_MS);
  }

  function renderBacktestHistory(snap) {
    const summary = $("backtest-summary");
    const table = $("bthistory-table");
    const list = $("bt-strategy-list");
    const records = snap && Array.isArray(snap.backtests) ? snap.backtests : [];
    if (summary) {
      if (snap && snap.ok === false) {
        summary.textContent = "history unavailable: " + String(snap.error || "unknown");
        summary.dataset.tone = "error";
      } else if (!records.length) {
        summary.textContent = "no completed backtests — launch one above";
        summary.dataset.tone = "ok";
      } else {
        summary.textContent = records.length + " completed backtest" +
          (records.length === 1 ? "" : "s") + " · newest first · select a row for details";
        summary.dataset.tone = "ok";
      }
    }
    // Refresh known run ids (drop rows no longer present) + strategy datalist.
    const seen = Object.create(null);
    const strategies = Object.create(null);
    for (const record of records) {
      btRecords[record.run_id] = record;
      seen[record.run_id] = true;
      if (record.strategy) strategies[record.strategy] = true;
      renderBacktestRow(record);
    }
    const rows = $("bthistory-rows");
    if (rows) {
      for (const tr of Array.from(rows.children)) {
        if (!seen[tr.dataset.run]) tr.remove();
      }
      // Reorder rows to match the (newest-first) snapshot order on every poll —
      // a newer run arriving on a later poll must move to the top, not strand at
      // the bottom (renderBacktestRow only ever appends a NEW row).
      for (const record of records) {
        const tr = rows.querySelector('[data-run="' + CSS.escape(String(record.run_id)) + '"]');
        if (tr) rows.appendChild(tr);
      }
    }
    if (list) {
      list.textContent = "";
      for (const name of Object.keys(strategies)) {
        const opt = el("option"); opt.value = name; list.appendChild(opt);
      }
    }
    if (table) table.hidden = records.length === 0;
    if (btSelected && !seen[btSelected]) closeBacktestDetail();
  }

  function renderBacktestRow(record) {
    const rows = $("bthistory-rows");
    if (!rows) return;
    const key = String(record.run_id);
    let tr = rows.querySelector('[data-run="' + CSS.escape(key) + '"]');
    if (!tr) {
      tr = el("tr");
      tr.dataset.run = key;
      tr.tabIndex = 0;
      tr.setAttribute("role", "button");
      tr.addEventListener("click", () => showBacktestDetail(key));
      tr.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showBacktestDetail(key); }
      });
      rows.appendChild(tr);
    }
    tr.textContent = "";
    const runTd = el("td", "bt-run-id"); runTd.textContent = key; tr.appendChild(runTd);
    const stratTd = el("td"); stratTd.textContent = String(record.strategy || "—"); tr.appendChild(stratTd);
    const paramTd = el("td", "bt-params"); paramTd.textContent = paramsText(record);
    paramTd.title = paramsText(record); tr.appendChild(paramTd);
    const win = record.run_window || {};
    const winTd = el("td"); winTd.textContent = String(win.start) + "–" + String(win.end); tr.appendChild(winTd);
    const m = record.metrics || {};
    const sharpeTd = el("td"); metricCellInto(sharpeTd, "ratio", m.sharpe); tr.appendChild(sharpeTd);
    const ddTd = el("td"); metricCellInto(ddTd, "pct", m.max_drawdown); tr.appendChild(ddTd);
    const retTd = el("td"); metricCellInto(retTd, "pct", m.annualized_return); tr.appendChild(retTd);
    const cmp = record.comparison || {};
    const vsTd = el("td"); metricCellInto(vsTd, "pct", cmp.excess_return); tr.appendChild(vsTd);
    tr.setAttribute("aria-selected", btSelected === key ? "true" : "false");
  }

  function showBacktestDetail(runId) {
    const record = btRecords[runId];
    const detail = $("backtest-detail");
    if (!record || !detail) return;
    btSelected = runId;
    for (const tr of Array.from($("bthistory-rows").children)) {
      tr.setAttribute("aria-selected", tr.dataset.run === runId ? "true" : "false");
    }
    detail.textContent = "";
    detail.hidden = false;

    // Header
    const head = el("div", "btd__head");
    const title = el("span", "btd__title"); title.textContent = String(record.run_id);
    const sub = el("span", "btd__sub");
    sub.textContent = String(record.strategy) + " · " + String(record.symbol) +
      " · window " + String((record.run_window || {}).start) + "–" + String((record.run_window || {}).end) +
      " · " + String(record.source) + " · " + String(record.code_version);
    const close = el("button", "btd__close"); close.type = "button"; close.textContent = "close ✕";
    close.addEventListener("click", closeBacktestDetail);
    head.append(title, sub, close);
    detail.appendChild(head);

    // Equity-curve chart (real inline SVG from the persisted equity points)
    const chartWrap = el("div", "btd__chartwrap");
    const chartLabel = el("span", "btd__section-label"); chartLabel.textContent = "Equity curve";
    const chartHost = el("div");
    const points = Array.isArray(record.equity_curve) ? record.equity_curve : [];
    chartHost.innerHTML = equityChartSVG(points, Number(record.starting_cash_minor));
    const readout = el("div", "btd__readout");
    readout.textContent = points.length
      ? points.length + " marks · start " + fmtMinor(points[0].equity_minor) +
        " → end " + fmtMinor(points[points.length - 1].equity_minor)
      : "no equity points recorded";
    chartWrap.append(chartLabel, chartHost, readout);
    detail.appendChild(chartWrap);
    wireChartHover(chartHost.querySelector("svg"), points, readout);

    // Metrics + benchmark comparison stat grid
    const metricsWrap = el("div", "btd__metrics");
    const M = record.metrics || {}, C = record.comparison || {};
    const stats = [
      ["Sharpe", "ratio", M.sharpe], ["Sortino", "ratio", M.sortino],
      ["Alpha", "ratio", M.alpha], ["Beta", "ratio", M.beta],
      ["Max drawdown", "pct", M.max_drawdown], ["Win rate", "pct", M.win_rate],
      ["Ann. return", "pct", M.annualized_return], ["Ann. vol", "pct", M.annualized_volatility],
      ["Excess vs " + String(C.benchmark_symbol || "SPY"), "pct", C.excess_return],
      ["Beta vs benchmark", "ratio", C.beta],
    ];
    for (const [label, kind, value] of stats) {
      const cell = el("div", "btstat");
      const k = el("span", "btstat__k"); k.textContent = label;
      const v = el("span", "btstat__v");
      if (value === null || value === undefined) { v.textContent = "—"; v.className = "btstat__v is-undef"; }
      else { v.textContent = fmt(kind, value); v.className = "btstat__v" + directionClass(kind, value); }
      cell.append(k, v); metricsWrap.appendChild(cell);
    }
    detail.appendChild(metricsWrap);

    // Full trade log
    const trades = Array.isArray(record.trade_log) ? record.trade_log : [];
    const tradesWrap = el("div", "bttrades");
    const tLabel = el("span", "btd__section-label");
    tLabel.textContent = "Trade log (" + trades.length + " fill" + (trades.length === 1 ? "" : "s") + ")";
    tradesWrap.appendChild(tLabel);
    if (trades.length) {
      const tbl = el("table");
      const thead = el("thead");
      const htr = el("tr");
      for (const h of ["Fill", "ts", "Qty", "Price", "Commission", "Slippage", "Spread"]) {
        const th = el("th"); th.textContent = h; htr.appendChild(th);
      }
      thead.appendChild(htr); tbl.appendChild(thead);
      const tbody = el("tbody");
      trades.forEach((f, i) => {
        const tr = el("tr");
        const cells = [
          String(i), String(f.ts), String(f.quantity), fmtMinor(f.price_minor),
          fmtMinor(f.commission_minor), fmtMinor(f.slippage_minor), fmtMinor(f.spread_impact_minor),
        ];
        for (const c of cells) { const td = el("td"); td.textContent = c; tr.appendChild(td); }
        tbody.appendChild(tr);
      });
      tbl.appendChild(tbody); tradesWrap.appendChild(tbl);
    }
    detail.appendChild(tradesWrap);
  }

  function closeBacktestDetail() {
    const detail = $("backtest-detail");
    if (detail) { detail.hidden = true; detail.textContent = ""; }
    btSelected = null;
    const rows = $("bthistory-rows");
    if (rows) for (const tr of Array.from(rows.children)) tr.setAttribute("aria-selected", "false");
  }

  // Build the equity curve as a single-series line+area SVG (dataviz: one series,
  // no legend, recessive axes, min/max markers). Only NUMBERS are interpolated
  // into the markup — every store-derived string is set via textContent elsewhere,
  // so a hostile run id / param can never reach innerHTML.
  function equityChartSVG(points, startingMinor) {
    const W = 680, H = 200, PL = 58, PR = 14, PT = 16, PB = 24;
    if (!points.length) {
      return '<svg class="eqchart" viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
        'aria-label="no equity points recorded"></svg>';
    }
    const xs = points.map((p) => Number(p.ts));
    const ys = points.map((p) => Number(p.equity_minor));
    let minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
    const base = isFinite(startingMinor) ? startingMinor : null;
    if (base !== null) { minY = Math.min(minY, base); maxY = Math.max(maxY, base); }
    const minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
    const spanY = (maxY - minY) || 1, spanX = (maxX - minX) || 1;
    const sx = (t) => PL + ((t - minX) / spanX) * (W - PL - PR);
    const sy = (v) => PT + (1 - (v - minY) / spanY) * (H - PT - PB);
    const pts = points.map((p) => [sx(Number(p.ts)), sy(Number(p.equity_minor))]);
    const linePath = pts.map((q, i) => (i ? "L" : "M") + q[0].toFixed(1) + " " + q[1].toFixed(1)).join(" ");
    const areaPath = "M" + pts[0][0].toFixed(1) + " " + (H - PB) + " " +
      pts.map((q) => "L" + q[0].toFixed(1) + " " + q[1].toFixed(1)).join(" ") +
      " L" + pts[pts.length - 1][0].toFixed(1) + " " + (H - PB) + " Z";
    const gy1 = sy(maxY).toFixed(1), gy0 = sy(minY).toFixed(1);
    let svg = '<svg class="eqchart" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' +
      "equity curve, " + points.length + " marks" + '">';
    svg += '<defs><linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="var(--accent)" stop-opacity="0.32"></stop>' +
      '<stop offset="100%" stop-color="var(--accent)" stop-opacity="0"></stop></linearGradient></defs>';
    svg += '<line class="eqchart__grid" x1="' + PL + '" y1="' + gy1 + '" x2="' + (W - PR) + '" y2="' + gy1 + '"></line>';
    svg += '<line class="eqchart__grid" x1="' + PL + '" y1="' + gy0 + '" x2="' + (W - PR) + '" y2="' + gy0 + '"></line>';
    if (base !== null) {
      const by = sy(base).toFixed(1);
      svg += '<line class="eqchart__base" x1="' + PL + '" y1="' + by + '" x2="' + (W - PR) + '" y2="' + by + '"></line>';
    }
    svg += '<text class="eqchart__label" x="6" y="' + (Number(gy1) + 3).toFixed(1) + '">' + fmtAxisMinor(maxY) + "</text>";
    svg += '<text class="eqchart__label" x="6" y="' + (Number(gy0) + 3).toFixed(1) + '">' + fmtAxisMinor(minY) + "</text>";
    svg += '<path class="eqchart__area" d="' + areaPath + '"></path>';
    svg += '<path class="eqchart__line" d="' + linePath + '"></path>';
    const hi = ys.indexOf(Math.max.apply(null, ys)), lo = ys.indexOf(Math.min.apply(null, ys));
    svg += '<circle class="eqchart__pt eqchart__hi" cx="' + sx(xs[hi]).toFixed(1) + '" cy="' + sy(ys[hi]).toFixed(1) + '" r="3.6"></circle>';
    svg += '<circle class="eqchart__pt eqchart__lo" cx="' + sx(xs[lo]).toFixed(1) + '" cy="' + sy(ys[lo]).toFixed(1) + '" r="3.6"></circle>';
    svg += '<circle class="eqchart__pt" cx="' + pts[pts.length - 1][0].toFixed(1) + '" cy="' + pts[pts.length - 1][1].toFixed(1) + '" r="3.6"></circle>';
    // A moving hover cursor + highlight, driven by wireChartHover.
    svg += '<line class="eqchart__cursor" x1="0" y1="' + PT + '" x2="0" y2="' + (H - PB) + '" style="opacity:0"></line>';
    svg += '<circle class="eqchart__pt eqchart__hover" r="4" style="opacity:0"></circle>';
    // Per-point invisible hit targets (data numbers only) for hover.
    for (let i = 0; i < pts.length; i++) {
      svg += '<circle class="eqchart__hit" cx="' + pts[i][0].toFixed(1) + '" cy="' + pts[i][1].toFixed(1) +
        '" r="10" fill="transparent" data-i="' + i + '"><title>t=' + xs[i] + " · " + fmtAxisMinor(ys[i]) + "</title></circle>";
    }
    svg += "</svg>";
    return svg;
  }

  function wireChartHover(svg, points, readout) {
    if (!svg || !points.length) return;
    const cursor = svg.querySelector(".eqchart__cursor");
    const hover = svg.querySelector(".eqchart__hover");
    const base = readout ? readout.textContent : "";
    svg.addEventListener("mouseover", (e) => {
      const hit = e.target.closest && e.target.closest(".eqchart__hit");
      if (!hit) return;
      const i = Number(hit.dataset.i);
      const p = points[i];
      if (!p) return;
      const cx = hit.getAttribute("cx"), cy = hit.getAttribute("cy");
      if (cursor) { cursor.setAttribute("x1", cx); cursor.setAttribute("x2", cx); cursor.style.opacity = "1"; }
      if (hover) { hover.setAttribute("cx", cx); hover.setAttribute("cy", cy); hover.style.opacity = "1"; }
      if (readout) readout.textContent = "t=" + p.ts + " · equity " + fmtMinor(p.equity_minor);
    });
    svg.addEventListener("mouseleave", () => {
      if (cursor) cursor.style.opacity = "0";
      if (hover) hover.style.opacity = "0";
      if (readout) readout.textContent = base;
    });
  }

  // ----- UI-5 Hot-Swap: manual-promotion control + Changeover-Console status //
  // CONTROL: two-step arm-then-confirm against the CONTRACT route on this same
  // runtime (never a /dashboard path). One in-flight request at a time; the
  // control is INERT unless a promotion candidate exists — a swap moves real
  // money and there must be nothing to promote before it can arm.
  //
  // FEEDBACK: the panel renders the four AC facts (manual promotion, the
  // demotion-pending state, the cool-down expiry, and the automatic-trigger
  // configuration) from the READ route /dashboard/api/hot-swap. Every live fact
  // is DEFERRED to its SRS-RESV producer today, so the pane draws each hatched
  // and never fabricates a swap state.
  //
  // FAIL CLOSED, everywhere. A changeover rung is resolved ONLY when the payload
  // carries a live value that agrees with its status; a deferred cell, a
  // missing/unknown status, a malformed payload, a non-OK response, a 404, an
  // unreachable/stalled endpoint — each renders UNKNOWN and disarms the control.
  const HOT_SWAP_ROUTE = "/api/v1/hot-swap?confirm=true";
  const HOT_SWAP_STATUS_ROUTE = "/dashboard/api/hot-swap";
  const HOT_ARM_WINDOW_MS = 5000;
  // A demote-before-promote swap can legitimately run for the SYS-49b demotion
  // timeout (60s default) before the server responds, so the client must not
  // abort inside that window and orphan an in-flight real-money mutation. 90s
  // comfortably exceeds it; a genuine abort is still treated as ambiguous below.
  const HOT_FETCH_TIMEOUT_MS = 90000;
  const HS_DIAL_CIRC = 2 * Math.PI * 80;   // cool-down dial arc (r=80)
  // The changeover ladder the pane shows before (and instead of) any observed
  // status. Order + phases mirror atp_dashboard.hotswap.CHANGEOVER_SEQUENCE; a
  // payload that disagrees is drift and is refused wholesale.
  const HS_PHASES = [
    ["demote_signals", "STOP NEW SIGNALS", "demote", false],
    ["demote_cancel", "CANCEL RESTING ORDERS", "demote", false],
    ["demote_liquidate", "LIQUIDATE TO FLAT", "demote", false],
    ["demotion_pending", "DEMOTION-PENDING (TIMEOUT)", "demote", true],
    ["promote", "PROMOTE CANDIDATE LIVE", "promote", false],
  ];
  const HS_MANUAL_LABEL = "Manual promotion";
  const HS_AUTO_KINDS = [
    ["drawdown_demotion", "Drawdown demotion"],
    ["top_ranked_promotion", "Top-ranked promotion"],
    ["highest_momentum_promotion", "Highest-momentum promotion"],
  ];
  let hotArmTimer = null;
  let hotInFlight = false;
  // The candidate id the control would promote — null means the control is INERT
  // (no candidate; the Reservoir ranking that names one is deferred).
  let hotCandidate = null;
  // The swap_id a 2xx designated; the pane must agree with it.
  let hotConfirmedSwapId = null;
  // Tri-state cool-down (true/false/null) from the last render — drives the
  // manual-during-cool-down confirmation warning (SYS-49e).
  let hotCooldownActive = null;
  // True while a fire RESULT (success or refusal) is showing, so a subsequent
  // poll's resting-caption refresh does not stomp it — a refusal the operator
  // must read cannot be overwritten by "candidate ready". Cleared on re-arm.
  let hotShowingResult = false;
  // The candidate id bound at ARM time. fireHotSwap posts THIS, never the
  // latest-polled hotCandidate: an operator who armed for A must never confirm a
  // swap for B if the Reservoir candidate changes inside the 5s arm window. The
  // disarm-on-upsert in renderHotSwap is the primary guard; posting the armed id
  // (not the live one) is the belt-and-suspenders identity bind.
  let hotArmedCandidate = null;
  // An ACCEPTED (2xx) swap awaiting durable confirmation, or null:
  //   {swapId, candidate, priorLive, promoted}. The contract response carries no
  // strategy id, so the outcome is asserted ONLY from a durable status read:
  //   promoted   -> live strategy IS this candidate (else MISMATCH);
  //   not promoted -> the blocked/demotion-pending state is observed.
  // While set, the control is INERT — a per-call outcome is not proof of the end
  // state, and a stale "clear" read must never re-enable a repeat swap.
  let hotPendingSwap = null;
  // Monotonic status-read generation. Every status poll captures the current
  // value and applies its response ONLY if still the latest; a mutation bumps it
  // too. This makes out-of-order polls harmless: a slow PRE-swap poll resolving
  // after the post-swap re-read cannot resurrect the stale candidate/cool-down.
  let hotPollSeq = 0;
  // Tri-state demotion-pending (true/false/null) from the last render. Promotion
  // is enabled ONLY on an EXPLICIT `false` (SRS-RESV-004 fail-closed): `true`
  // blocks (pending resolution) and `null` (unknown — deferred, or a partial
  // source outage) also blocks, because the UI cannot prove there is no pending
  // demotion timeout.
  let hotDemotionPending = null;
  // Whether the last status snapshot was fully readable (`ok === true`). Source
  // legs fail independently, so a readable candidate with an unreadable
  // live/demotion leg (ok:false) must NOT be actionable.
  let hotStatusOk = false;
  // Whether an observed changeover is in progress or stuck: any resolved rung is
  // PENDING / BLOCKED / FAILED. A swap must not be startable while a prior
  // demote→promote changeover is incomplete (demote-before-promote / single-live).
  let hotChangeoverActive = false;
  // The resolved current live strategy id, or null when unknown. Promotion must
  // DEMOTE the current live strategy first (SRS-RESV-004), so the control is
  // inert until we know what will be demoted — an unknown live slot is not
  // actionable.
  let hotLiveStrategy = null;

  function hsBtn() { return $("hs-btn"); }
  function hsStatus(text, tone) {
    const s = $("hs-status");
    if (s) { s.textContent = text; s.dataset.tone = tone || ""; }
  }
  function hsState(state) {
    const root = $("hs");
    if (root) root.dataset.state = state;
  }
  function hotArmed() {
    const b = hsBtn();
    return !!b && b.dataset.armed === "true";
  }
  function hsRestingCaption() {
    if (hotDemotionPending === true) {
      return "DEMOTION-PENDING — promotion blocked until manual resolution (SRS-RESV-004)";
    }
    if (!hotCandidate) {
      return "no promotion candidate — Reservoir ranking deferred (SRS-RESV-002)";
    }
    if (hotChangeoverActive) {
      // A demote→promote changeover is already in progress or stuck.
      return "changeover in progress — promotion held (SRS-RESV-004/005)";
    }
    if (!hotLiveStrategy) {
      // Promotion demotes the current live strategy first (SRS-RESV-004) — hold
      // the control until we know what will be demoted.
      return "promotion held — current live strategy unknown (SRS-RESV-005)";
    }
    if (hotDemotionPending !== false || hotCooldownActive === null || !hotStatusOk) {
      // A candidate exists but some swap-safety state cannot be proven clear (an
      // unverified demotion-pending state, an unknown cool-down (SRS-RESV-006), or
      // a partial source outage) — hold rather than promote against unknown truth.
      return "promotion held — swap-safety state unverified (SRS-RESV-004/006)";
    }
    return "candidate " + hotCandidate + " ready — arm to promote";
  }

  // The control is actionable ONLY when the FULL swap-safety picture is known and
  // clear: a candidate to promote, a KNOWN current live strategy to demote
  // (SRS-RESV-004), nothing in flight, a fully readable status, an EXPLICIT
  // not-pending demotion state, a KNOWN cool-down state (SRS-RESV-006), and no
  // changeover already in progress/blocked (demote-before-promote / single-live).
  function hotActionable() {
    return (
      !!hotCandidate &&
      !!hotLiveStrategy &&
      !hotInFlight &&
      !hotPendingSwap && // an accepted swap awaiting durable confirmation blocks a repeat
      hotStatusOk &&
      hotDemotionPending === false &&
      hotCooldownActive !== null &&
      !hotChangeoverActive
    );
  }

  // The control's actionable state, recomputed on every render: inert unless a
  // candidate exists and no request is in flight. Only touches the caption while
  // at rest — never stomps an armed / firing / result caption.
  function updateHotButton() {
    const b = hsBtn();
    if (!b) return;
    b.disabled = !hotActionable();
    if (!hotArmed() && !hotInFlight && !hotConfirmedSwapId && !hotShowingResult) {
      hsStatus(hsRestingCaption(), "");
    }
  }

  function disarmHotSwap(restoreResting) {
    if (hotArmTimer) { clearTimeout(hotArmTimer); hotArmTimer = null; }
    hotArmedCandidate = null;   // the staged target is released
    const b = hsBtn();
    if (b) { b.dataset.armed = "false"; b.textContent = "PROMOTE CANDIDATE"; }
    hsState(hotConfirmedSwapId ? "fired" : "resting");
    if (restoreResting) hsStatus(hsRestingCaption(), "");
  }

  function armHotSwap() {
    hotShowingResult = false;   // operator re-engaged; the arm caption takes over
    hotPendingSwap = null; // a fresh arm supersedes any awaiting confirmation
    hotArmedCandidate = hotCandidate;   // bind the target id at arm time
    const b = hsBtn();
    if (b) { b.dataset.armed = "true"; b.textContent = "CONFIRM PROMOTE"; }
    hsState("armed");
    // Fail closed on the cool-down warning: an UNKNOWN cool-down does NOT
    // suppress the confirmation (SYS-49e). Only a KNOWN-active cool-down escalates.
    const warn = hotCooldownActive === true
      ? " · COOL-DOWN ACTIVE — manual swap during cool-down (SYS-49e)"
      : (hotCooldownActive === null ? " · cool-down state unknown (SRS-RESV-006)" : "");
    hsStatus("ARMED — confirm within 5s to demote live and promote " + hotCandidate + warn, "armed");
    if (hotArmTimer) clearTimeout(hotArmTimer);
    hotArmTimer = setTimeout(() => disarmHotSwap(true), HOT_ARM_WINDOW_MS);
  }

  async function fireHotSwap() {
    // Fire the candidate bound at ARM time, never the latest-polled one — a
    // candidate that changed under an armed control is disarmed by
    // renderHotSwap, so reaching here with a stale target should be impossible;
    // posting the armed id (not hotCandidate) makes that guarantee local too.
    const candidate = hotArmedCandidate;
    if (hotInFlight || !candidate) return;
    hotInFlight = true;
    // A mutation invalidates any status read issued before it: bump the poll
    // generation so a pre-swap poll still in flight cannot apply post-swap.
    hotPollSeq++;
    if (hotArmTimer) { clearTimeout(hotArmTimer); hotArmTimer = null; }
    const b = hsBtn();
    if (b) { b.disabled = true; b.dataset.armed = "false"; b.textContent = "PROMOTING…"; }
    hsState("firing");
    hsStatus("requesting hot-swap for " + candidate + "…", "pending");
    try {
      const res = await fetch(HOT_SWAP_ROUTE, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ candidate_strategy_id: candidate, confirm: true }),
        signal: AbortSignal.timeout(HOT_FETCH_TIMEOUT_MS),
      });
      const body = await res.json().catch(() => null);
      if (res.ok) {
        // Any 2xx means the swap was ACCEPTED, so state may have mutated. Capture
        // the live strategy AT swap time and hold the control inert (hotPendingSwap)
        // until the durable status is causally correlated with the outcome — a
        // stale "clear" read must never re-enable a repeat swap.
        const swapId = body && typeof body.swap_id === "string" ? body.swap_id.trim() : "";
        const promotion = body && typeof body.promotion_state === "string" ? body.promotion_state : "UNKNOWN";
        const demotion = body && typeof body.demotion_state === "string" ? body.demotion_state : "UNKNOWN";
        const promoted = !!swapId && promotion === "PROMOTED";
        if (swapId) hotConfirmedSwapId = swapId;
        hotPendingSwap = {
          swapId: swapId || null,
          candidate: candidate,
          priorLive: hotLiveStrategy,
          promoted: promoted,
        };
        if (promoted) {
          hsStatus("swap " + swapId + " reported PROMOTED — verifying live strategy is " +
            candidate + "…", "pending");
        } else if (swapId) {
          // Demote-before-promote may be pending or blocked (SYS-49b). Never a success.
          hsStatus("swap " + swapId + ": demotion " + demotion + ", promotion " + promotion +
            " — NOT promoted; awaiting durable confirmation of the block (SRS-RESV-004)", "pending");
        } else {
          hsStatus("swap accepted but carries no swap_id — cannot confirm what ran; " +
            "awaiting durable status (SRS-RESV-004)", "pending");
        }
      } else {
        // A refusal (non-2xx) mutated nothing — no pending guard; retry is allowed.
        const err = body && body.error ? body.error : {};
        const owner = err.detail && err.detail.owner ? " · owner " + String(err.detail.owner) : "";
        hsStatus("REFUSED " + res.status + " " + String(err.type || err.category || "UNKNOWN") + owner, "error");
      }
    } catch (error) {
      // A timeout / network error is AMBIGUOUS: the swap may still be in flight
      // on the server (a demotion can run for the SYS-49b timeout). Treat it as an
      // accepted-unknown swap and hold the control inert until durable status
      // proves a terminal state — never re-enable a second swap on a stale-clear
      // read while the first demotion may still be running.
      hotPendingSwap = {
        swapId: null,
        candidate: candidate,
        priorLive: hotLiveStrategy,
        promoted: false,
        ambiguous: true,
      };
      hsStatus("hot-swap request did not complete (" + String(error) + ") — the swap MAY be " +
        "in flight; awaiting durable confirmation before another swap (SRS-RESV-004)", "error");
    }
    // The result caption (success or refusal) is now the operator's to read; the
    // re-read below must not stomp it with the resting caption.
    hotShowingResult = true;
    hotInFlight = false;
    // Fail closed against a rapid repeat: the pre-swap candidate + cool-down are
    // now STALE (a swap changes the live strategy, starts the cool-down, and
    // moves the candidate). Clear them and keep the control DISABLED — it is not
    // actionable again until the AWAITED durable re-read re-derives a current
    // candidate + cool-down. The button stays disabled from the fire above; do
    // NOT re-enable it here.
    hotCandidate = null;
    hotCooldownActive = null;
    disarmHotSwap(false);
    await pollHotSwapOnce();
  }

  function onHotTrigger() {
    // In-flight guard first; then the candidate gate; then arm-then-fire.
    if (hotInFlight) return;
    if (!hotCandidate) return;
    if (!hotArmed()) { armHotSwap(); return; }
    fireHotSwap();
  }

  // ----- UI-5 status pane rendering (fail-closed) ------------------------- //

  function hsBuildDialTicks() {
    const g = $("hs-dial-ticks");
    if (!g) return;
    g.textContent = "";
    const NS = "http://www.w3.org/2000/svg";
    for (let i = 0; i < 7; i++) {   // SYS-49e: 7-day cool-down, one tick per day
      const a = (i / 7) * 2 * Math.PI - Math.PI / 2;
      const line = document.createElementNS(NS, "line");
      line.setAttribute("x1", String(100 + 88 * Math.cos(a)));
      line.setAttribute("y1", String(100 + 88 * Math.sin(a)));
      line.setAttribute("x2", String(100 + 95 * Math.cos(a)));
      line.setAttribute("y2", String(100 + 95 * Math.sin(a)));
      line.setAttribute("class", "hs__tick");
      g.appendChild(line);
    }
  }

  function hsChip(kind, label, enabled, defaultEnabled, isManual) {
    const chip = el("span", "hs__chip");
    chip.dataset.kind = kind;
    chip.dataset.manual = isManual ? "true" : "false";
    chip.dataset.state = isManual ? "manual" : (enabled === true ? "on" : (enabled === false ? "off" : "deferred"));
    const sw = el("span", "hs__chip-sw"); sw.setAttribute("aria-hidden", "true");
    const name = el("span", "hs__chip-label"); name.textContent = label;
    const meta = el("span", "hs__chip-meta");
    meta.textContent = isManual ? "always available"
      : (enabled === true ? "ENABLED"
        : enabled === false ? "disabled"
        : "default " + (defaultEnabled ? "on" : "off") + " · SRS-RESV-003");
    chip.append(sw, name, meta);
    return chip;
  }

  function renderTriggers(snap) {
    const wrap = $("hs-triggers");
    if (!wrap) return;
    wrap.textContent = "";
    wrap.appendChild(hsChip("manual", HS_MANUAL_LABEL, null, true, true));
    const live = snap && Array.isArray(snap.auto_triggers_live) ? snap.auto_triggers_live : [];
    const byKind = {};
    for (const t of live) { if (t && typeof t === "object") byKind[t.kind] = t; }
    for (const [kind, label] of HS_AUTO_KINDS) {
      const lt = byKind[kind];
      const enabled = lt ? hotSwapCellBool(lt.enabled) : null;
      wrap.appendChild(hsChip(kind, label, enabled, false, false));
    }
  }

  function hsSlot(valId, srcId, value, owner) {
    const v = $(valId);
    if (v) {
      const present = typeof value === "string" && value;
      v.textContent = present ? value : "—";
      v.dataset.state = present ? "live" : "deferred";
    }
    const s = $(srcId);
    if (s) s.textContent = (typeof value === "string" && value) ? "live" : ("awaiting " + owner);
  }

  function renderCooldown(snap) {
    const dial = $("hs-dial");
    const arc = $("hs-dial-arc");
    const valueEl = $("hs-cooldown-value");
    const subEl = $("hs-cooldown-sub");
    const cd = snap && typeof snap.cooldown === "object" && snap.cooldown ? snap.cooldown : {};
    const expires = hotSwapCellValue(cd.expires_at);
    const started = hotSwapCellValue(cd.started_at);
    const inEffect = hotSwapCellBool(cd.in_effect);
    const days = typeof snap.cooldown_days_default === "number" ? snap.cooldown_days_default : 7;
    const c = hotSwapCooldown(typeof expires === "string" ? expires : null, Date.now(), days);
    // The dial keys off the KNOWN cool-down flag first: an explicit not-in-effect
    // is READY (no active cool-down), not a hatched "unknown". Only an unknown
    // (null) in_effect renders deferred.
    let dialState = c.state;
    if (c.state === "deferred" && inEffect === false) dialState = "expired";
    if (dial) dial.dataset.state = dialState;
    if (arc) arc.style.strokeDashoffset = String(dialState === "active" ? HS_DIAL_CIRC * (1 - c.fraction) : 0);
    if (valueEl) valueEl.textContent = dialState === "active" ? c.label : dialState === "expired" ? "READY" : c.label;
    if (subEl) {
      subEl.textContent = dialState === "deferred"
        ? "cool-down state — awaiting SRS-RESV-006"
        : dialState === "expired"
          ? "no active cool-down — automatic triggers may fire"
          : (typeof started === "string" && started ? "since " + started + " · " : "") + "expires " + expires;
    }
    return inEffect;
  }

  function hsRung(phase, label, stage, branch, status, detail, owner) {
    const li = el("li", "hs__rung");
    li.dataset.phase = phase;
    li.dataset.stage = stage;
    li.dataset.branch = branch ? "true" : "false";
    li.dataset.status = status;
    const node = el("span", "hs__node"); node.setAttribute("aria-hidden", "true");
    const body = el("div", "hs__rbody");
    const row = el("div", "hs__rrow");
    const name = el("span", "hs__rlabel"); name.textContent = label;
    const badge = el("span", "hs__rbadge"); badge.dataset.status = status; badge.textContent = status;
    row.append(name, badge);
    const det = el("span", "hs__rdetail"); det.textContent = detail;
    body.append(row, det);
    if (owner) { const own = el("span", "hs__rowner"); own.textContent = "awaiting " + owner; body.appendChild(own); }
    li.append(node, body);
    return li;
  }

  function hsTrackClear(status, detail) {
    const track = $("hs-track");
    if (!track) return;
    track.textContent = "";
    for (const [phase, label, stage, branch] of HS_PHASES) {
      track.appendChild(hsRung(phase, label, stage, branch, status, detail, null));
    }
  }

  // The single fail-closed clear: dial deferred, slots deferred, every chip's
  // live state deferred, every rung UNKNOWN, control INERT. Used by every
  // degraded branch — a partial clear that leaves one resolved fact on screen is
  // exactly the bug this guards against.
  function hsUnknown(reason, tone) {
    hsTrackClear("UNKNOWN", "not observed");
    const dial = $("hs-dial");
    if (dial) dial.dataset.state = "deferred";
    const arc = $("hs-dial-arc"); if (arc) arc.style.strokeDashoffset = "0";
    const cv = $("hs-cooldown-value"); if (cv) cv.textContent = "— —";
    const cs = $("hs-cooldown-sub"); if (cs) cs.textContent = "cool-down — awaiting SRS-RESV-006";
    hsSlot("hs-live", "hs-live-src", null, "SRS-RESV-005");
    hsSlot("hs-candidate", "hs-candidate-src", null, "SRS-RESV-002");
    renderTriggers({});
    hotCandidate = null;
    hotCooldownActive = null;
    hotDemotionPending = null;   // unknown demotion state is NOT actionable
    hotStatusOk = false;         // a degraded/unreadable status is never actionable
    hotChangeoverActive = false; // no observed changeover in a cleared pane
    hotLiveStrategy = null;      // unknown live strategy is NOT actionable
    // A degraded status is an interval where Hot-Swap state is UNKNOWN: any staged
    // confirmation must be dropped, not merely disabled — otherwise a recovery
    // within the 5s arm window would re-enable CONFIRM PROMOTE without a fresh arm.
    disarmHotSwap(false);
    updateHotButton();
    const note = $("hs-note");
    if (note) { note.textContent = reason; note.dataset.tone = tone || "warn"; }
    if (!hotInFlight) hsState(hotConfirmedSwapId ? "fired" : "resting");
  }

  function renderHotSwap(snap) {
    if (!snap || typeof snap !== "object") {
      hsUnknown("hot-swap status payload malformed — treating every fact as UNKNOWN", "error");
      return;
    }
    const seq = snap.changeover_sequence;
    // Schema drift is an honesty problem: if the ladder does not match the phase
    // order this client knows, refuse the whole payload rather than render a
    // partial changeover.
    const shaped = Array.isArray(seq) && seq.length === HS_PHASES.length &&
      seq.every((leg, i) => leg && typeof leg === "object" &&
        leg.phase === HS_PHASES[i][0] && typeof leg.status === "string");
    if (!shaped) {
      hsUnknown("hot-swap status payload does not match the known changeover contract — treating every fact as UNKNOWN", "error");
      return;
    }
    // An observed changeover is in progress/stuck if any RESOLVED rung is
    // PENDING / BLOCKED / FAILED — the control must be inert while one is
    // incomplete (demote-before-promote / single-live). Deferred (UNKNOWN) rungs
    // do not count; a fully-DONE changeover is complete, not active.
    hotChangeoverActive = seq.some((leg) => {
      const resolved =
        typeof leg.value === "string" && leg.value === leg.status && leg.status !== "UNKNOWN";
      return resolved && (leg.status === "PENDING" || leg.status === "BLOCKED" || leg.status === "FAILED");
    });
    const track = $("hs-track");
    if (track) {
      track.textContent = "";
      seq.forEach((leg, i) => {
        // A rung is resolved ONLY when the payload carries a live value that
        // AGREES with the status. A deferred cell (value null) or any
        // disagreement renders UNKNOWN.
        const resolved = typeof leg.value === "string" && leg.value === leg.status && leg.status !== "UNKNOWN";
        const status = resolved ? leg.status : "UNKNOWN";
        const detail = typeof leg.detail === "string" && leg.detail ? leg.detail : "not observed";
        const owner = resolved ? null : (typeof leg.owner === "string" ? leg.owner : null);
        track.appendChild(hsRung(HS_PHASES[i][0], HS_PHASES[i][1], HS_PHASES[i][2], HS_PHASES[i][3], status, detail, owner));
      });
    }

    hotCooldownActive = renderCooldown(snap);
    const liveStrategy = hotSwapCellValue(snap.current_live_strategy_id);
    hotLiveStrategy = (typeof liveStrategy === "string" && liveStrategy) ? liveStrategy : null;
    hsSlot("hs-live", "hs-live-src", liveStrategy, "SRS-RESV-005");
    const candidate = hotSwapCellValue(snap.promotion_candidate);
    hotCandidate = (typeof candidate === "string" && candidate) ? candidate : null;
    hotDemotionPending = hotSwapCellBool(snap.demotion_pending);
    hotStatusOk = snap.ok === true;
    hsSlot("hs-candidate", "hs-candidate-src", hotCandidate, "SRS-RESV-002");
    // Disarm any staged confirmation that has become unsafe: the candidate
    // changed/was removed under an armed control (a staged confirm would demote
    // live and promote the WRONG strategy), or the control is no longer
    // actionable (a KNOWN demotion-pending state, an unknown demotion state, or a
    // partial source outage — SRS-RESV-004 fail-closed). Stale truth left
    // ACTIONABLE is the class this pane must not have.
    if (hotArmed() && (hotArmedCandidate !== hotCandidate || !hotActionable())) {
      disarmHotSwap(true);
    }
    // Resolve an accepted swap against the DURABLE end state BEFORE deciding the
    // button state. The live strategy captured at swap time separates "not yet
    // reflected" (a stale/uncorrelated pre-swap snapshot) from a real outcome — a
    // stale read must neither confirm, declare a mismatch, nor re-enable the
    // control. While hotPendingSwap is set, hotActionable() holds it inert.
    if (hotPendingSwap) {
      const sw = hotPendingSwap.swapId || "(no swap_id)";
      const want = hotPendingSwap.candidate;
      const prior = hotPendingSwap.priorLive;
      if (hotLiveStrategy === want) {
        // The candidate IS live now — the swap promoted, whatever the response said
        // (covers an ambiguous timeout whose swap actually completed).
        hsStatus("promoted " + want + " live · swap " + sw, "fired");
        hotPendingSwap = null;
      } else if (hotDemotionPending === true || hotChangeoverActive) {
        // A blocked / demotion-pending state is now observed — a terminal outcome.
        // The demotion/changeover gates keep the control inert past this.
        hsStatus("swap " + sw + ": promotion blocked — resolve the demotion-pending " +
          "changeover before another swap (SRS-RESV-004)", "error");
        hotPendingSwap = null;
      } else if (hotPendingSwap.promoted && hotLiveStrategy && hotLiveStrategy !== prior) {
        // The response explicitly claimed to promote `want`, but a DIFFERENT
        // strategy is live — a wrong-live-strategy mismatch.
        hsStatus("MISMATCH: swap " + sw + " reported PROMOTED for " + want +
          " but the live strategy is " + hotLiveStrategy + " — verify before acting", "error");
        hotPendingSwap = null;
      } else {
        // Not yet reflected (live still prior / unknown, no block observed). Hold
        // inert; a stale "clear" read must not clear the guard.
        const caption = hotPendingSwap.ambiguous
          ? "swap outcome unknown (request did not complete) — awaiting durable confirmation " +
            "before another swap (SRS-RESV-004)"
          : hotPendingSwap.promoted
            ? "swap " + sw + " reported PROMOTED — awaiting durable confirmation that " +
              want + " is live (SRS-RESV-005)"
            : "swap " + sw + " did NOT promote — awaiting durable confirmation of the " +
              "blocked changeover (SRS-RESV-004)";
        hsStatus(caption, "pending");
      }
    }
    renderTriggers(snap);
    updateHotButton();

    const note = $("hs-note");
    if (note) {
      const errors = Array.isArray(snap.errors) ? snap.errors.filter((e) => typeof e === "string") : [];
      const demotionPending = hotSwapCellBool(snap.demotion_pending);
      if (errors.length) {
        note.textContent = errors.join(" · ");
        note.dataset.tone = "error";
      } else if (demotionPending === true) {
        note.textContent = "DEMOTION-PENDING — a swap timed out before flat; promotion is blocked until manual resolution (SRS-RESV-004)";
        note.dataset.tone = "error";
      } else {
        note.textContent = "every live Hot-Swap fact is deferred to its SRS-RESV producer; the control POSTs to the contract route and renders the runtime's response verbatim";
        note.dataset.tone = "warn";
      }
    }
    if (!hotInFlight) hsState(hotConfirmedSwapId ? "fired" : "resting");
  }

  async function pollHotSwapOnce() {
    const seq = ++hotPollSeq;   // this read's generation
    // Discard a response that a newer read (or a mutation) has superseded — an
    // out-of-order slow poll must never resurrect stale candidate/cool-down state.
    const superseded = () => seq !== hotPollSeq;
    try {
      const res = await fetch(HOT_SWAP_STATUS_ROUTE, {
        cache: "no-store",
        signal: AbortSignal.timeout(POLL_MS),
      });
      if (superseded()) return;
      if (res.ok) {
        let body = null;
        try { body = await res.json(); } catch (_e) { body = null; }
        if (superseded()) return;
        renderHotSwap(body);
      } else if (res.status === 404) {
        hsUnknown("hot-swap status not mounted — UI-5 provider not composed on this runtime", "warn");
      } else {
        hsUnknown("hot-swap status unavailable (HTTP " + res.status + ") — every fact UNKNOWN", "error");
      }
    } catch (_e) {
      if (superseded()) return;
      hsUnknown("hot-swap status endpoint unreachable — every fact UNKNOWN", "error");
    }
  }

  async function pollHotSwap() {
    await pollHotSwapOnce();
    setTimeout(pollHotSwap, POLL_MS);
  }

  function initHotSwap() {
    hsBuildDialTicks();
    // Paint the fail-closed pane before the first poll resolves.
    hsUnknown("awaiting hot-swap status…", "warn");
    const b = hsBtn();
    if (b) b.addEventListener("click", onHotTrigger);
  }

  // ----- SRS-LOG-001 persistent logs (SyRS SYS-61 system + SYS-38 strategy) //
  // Two classes, two stores, two tables — the pane never merges them. Records
  // arrive newest-first from /dashboard/api/logs, and a record published on the
  // subscribed LOGS WebSocket channel is prepended to its own class's buffer
  // (see the SUBSCRIBE frame in connect() — a handler for a channel the client
  // never subscribed to would leave this pane silently poll-only).
  //
  // The honesty rule that shapes every branch below: an empty table means "the
  // store was read and matched nothing", and it is rendered with that wording.
  // `records: null` (unreadable / unmounted) is a DIFFERENT state and renders
  // as an explicit error — a log pane that shows "0 records" for a store it
  // could not read is the one failure that makes an audit surface worthless.
  const LOGS_ROUTE = "/dashboard/api/logs";
  const LOG_CLASSES = ["system", "strategy"];
  //: Client-side minimum-severity filter, in the SyRS SYS-61 order.
  const LOG_SEVERITY_ORDER = ["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"];
  //: Newest-first buffers, so a WS event can be prepended without a re-poll.
  const logBuffers = { system: null, strategy: null };
  //: Live LOGS events not yet seen in a REST snapshot, newest-first.
  //
  // The two feeds race: a poll that STARTED before a WebSocket event can resolve
  // AFTER it, and replacing the buffer with that older snapshot would erase the
  // event from the pane until some later poll happened to include it. An audit
  // event that appears and then vanishes is worse than one that arrives late, so
  // live events are held here and merged over every snapshot until the snapshot
  // itself carries them.
  const logLiveEvents = { system: [], strategy: [] };
  //: Cap on that hold-back list, so a persistently failing poll cannot grow it
  //: without bound. Oldest pending events are dropped first — they are the ones
  //: a snapshot is most likely to already contain.
  const LOG_LIVE_EVENT_CAP = 200;
  //: Whether each class's store FILE exists — "absent" and "empty" are
  //: different facts and are rendered differently.
  const logStorePresent = { system: null, strategy: null };
  let logMinSeverity = "";

  function setLogsDot(state, title) {
    const dot = $("fresh-logs");
    if (dot) { dot.dataset.state = state; dot.title = title; }
  }

  // Identity of a rendered event, for matching a live event against a snapshot.
  //
  // record_id is the persisted line's own identity and is what this must key on:
  // audit VALUES repeat by design — a retried operation writes the same message
  // with the same correlation id, and the rendered timestamp is only
  // milliseconds — so keying on them would treat two real events as one and drop
  // the newer from the pane. The value fallback exists only for a payload
  // without an id (an older server); it is strictly worse and never preferred.
  function logEventKey(event) {
    if (event && event.record_id) return "id\u0000" + String(event.record_id);
    return [
      "value", event.timestamp, event.severity, event.source, event.event_type,
      event.message, event.correlation_id, event.log_class, event.strategy_id,
    ].join("\u0000");
  }

  function severityRank(value) {
    const idx = LOG_SEVERITY_ORDER.indexOf(String(value || "").toUpperCase());
    return idx === -1 ? -1 : idx;
  }

  function passesSeverity(record) {
    if (!logMinSeverity) return true;
    const rank = severityRank(record && record.severity);
    // An unrecognised severity is NOT silently filtered out: a record whose
    // severity we cannot rank is shown, never hidden by a filter it may well
    // have satisfied.
    return rank === -1 || rank >= severityRank(logMinSeverity);
  }

  function initLogs() {
    const select = $("logs-severity");
    if (select) {
      for (const sev of LOG_SEVERITY_ORDER) {
        const opt = el("option");
        opt.value = sev;
        opt.textContent = sev + "+";
        select.appendChild(opt);
      }
      select.addEventListener("change", () => {
        logMinSeverity = select.value;
        for (const cls of LOG_CLASSES) renderLogClass(cls);
        renderLogSummary();
      });
    }
    for (const cls of LOG_CLASSES) {
      setLogClassMessage(cls, "awaiting log store…", "warn");
    }
  }

  function setLogClassMessage(cls, text, tone) {
    const empty = $("logs-" + cls + "-empty");
    const table = $("logs-" + cls + "-table");
    const rows = $("logs-" + cls + "-rows");
    if (rows) rows.textContent = "";
    if (table) table.hidden = true;
    if (empty) {
      empty.hidden = false;
      empty.textContent = text;
      if (tone) { empty.dataset.tone = tone; } else { delete empty.dataset.tone; }
    }
  }

  function renderLogRow(record) {
    const tr = el("tr");
    const time = el("td", "logs__time");
    time.textContent = String(record.timestamp || "—");
    tr.appendChild(time);
    const sevTd = el("td");
    const sev = el("span", "logs__sev");
    sev.dataset.sev = String(record.severity || "");
    sev.textContent = String(record.severity || "—");
    sevTd.appendChild(sev);
    tr.appendChild(sevTd);
    const source = el("td");
    // `source` is always the literal "strategy" on a strategy line, so the id is
    // what makes it attributable — without it every Reservoir strategy's lines
    // look identical in this table.
    source.textContent = record.strategy_id
      ? String(record.source || "—") + " · " + String(record.strategy_id)
      : String(record.source || "—");
    tr.appendChild(source);
    const event = el("td");
    event.textContent = String(record.event_type || "—");
    tr.appendChild(event);
    const message = el("td", "logs__msg");
    message.textContent = String(record.message || "—");
    tr.appendChild(message);
    const corr = el("td", "logs__corr");
    corr.textContent = String(record.correlation_id || "—");
    tr.appendChild(corr);
    return tr;
  }

  function renderLogClass(cls) {
    const buffer = logBuffers[cls];
    if (!Array.isArray(buffer)) return;  // unreadable/unmounted state already painted
    const shown = buffer.filter(passesSeverity);
    const table = $("logs-" + cls + "-table");
    const rows = $("logs-" + cls + "-rows");
    const empty = $("logs-" + cls + "-empty");
    if (rows) {
      rows.textContent = "";
      for (const record of shown) rows.appendChild(renderLogRow(record));
    }
    if (table) table.hidden = shown.length === 0;
    if (empty) {
      empty.hidden = shown.length !== 0;
      // Wording matters: this is an observation about the READ, not a claim
      // that the system produced nothing. And a MISSING store file is a
      // different fact again — a trail that was never created, moved, or
      // deleted must not render like a present-but-quiet one.
      if (logStorePresent[cls] === false) {
        empty.dataset.tone = "warn";
        empty.textContent = "store file does not exist — no record has ever been written here, " +
          "or the trail was moved. This is NOT an empty trail.";
      } else {
        delete empty.dataset.tone;
        empty.textContent = logMinSeverity
          ? "store read — no record at or above " + logMinSeverity
          : "store read — no records in this trail yet";
      }
    }
  }

  function renderLogClassCell(cls, cell) {
    const store = $("logs-" + cls + "-store");
    const count = $("logs-" + cls + "-count");
    if (store) store.textContent = (cell && cell.store) ? String(cell.store) : "—";
    // Fail closed: anything but an explicit ok:true with a real list is an
    // unreadable trail. `records: null` must never become [].
    if (!cell || cell.ok !== true || !Array.isArray(cell.records)) {
      logBuffers[cls] = null;
      logLiveEvents[cls] = [];
      logStorePresent[cls] = cell && cell.store_present === false ? false : null;
      const reason = (cell && cell.error) ? String(cell.error) : "store unreadable";
      // A MISSING store and an UNREADABLE one are different operator problems:
      // one is "the trail you configured is not there", the other is "the trail
      // is there but I cannot parse it". Neither is an empty log.
      const missing = cell && cell.store_present === false;
      setLogClassMessage(
        cls,
        (missing ? "log store MISSING: " : "log store unavailable: ") + reason +
          " — this is NOT an empty trail",
        "error"
      );
      if (count) { count.textContent = missing ? "missing" : "unreadable"; count.dataset.tone = "error"; }
      return;
    }
    // Merge, never replace: keep any live event this snapshot does not yet
    // contain (the poll may have started before it arrived), and drop the ones
    // it does — those are now durable history rather than pending overlay.
    const snapshot = cell.records.slice();
    const snapshotKeys = new Set(snapshot.map(logEventKey));
    const pending = logLiveEvents[cls].filter((event) => !snapshotKeys.has(logEventKey(event)));
    logLiveEvents[cls] = pending;
    // Ordered by timestamp, not by which path delivered it. Concatenating the
    // held-back events in front assumed they were always newer, which stops
    // being true once a burst exceeds the snapshot page: an event that fell off
    // the page would be pinned above strictly newer records, and stay there,
    // because a page capped at the newest N can never contain it again. The
    // table says newest-first, so it sorts newest-first. The sort is stable, so
    // records sharing a timestamp keep their delivery order.
    logBuffers[cls] = pending.concat(snapshot).sort((a, b) =>
      String(b && b.timestamp || "").localeCompare(String(a && a.timestamp || ""))
    );
    logStorePresent[cls] = cell.store_present === false ? false : true;
    if (count) {
      delete count.dataset.tone;
      // A tail-read cell reports no total (matched: null) — it read a page, not
      // the trail. Showing a made-up total would be the pane inventing a fact;
      // "newest N" is what it actually knows.
      if (cell.matched === null || cell.matched === undefined) {
        // Say what was verified. "newest N" alone would let a reader take the
        // green pane as a clean audit history; only the page was checked.
        count.textContent = cell.integrity_scope === "page"
          ? "newest " + cell.records.length + " · older history not verified here"
          : "newest " + cell.records.length;
      } else {
        count.textContent = cell.truncated
          ? cell.records.length + " of " + cell.matched
          : String(cell.matched);
      }
    }
    renderLogClass(cls);
  }

  function renderLogCoverage(coverage) {
    const strip = $("logs-coverage");
    if (!strip || !Array.isArray(coverage)) return;
    strip.textContent = "";
    for (const entry of coverage) {
      const chip = el("span", "logs__cov");
      chip.dataset.state = String(entry.state || "deferred");
      const owners = Array.isArray(entry.owners) ? entry.owners : [];
      const missing = Array.isArray(entry.unproduced_event_types)
        ? entry.unproduced_event_types
        : [];
      chip.textContent = String(entry.source || "?") +
        (missing.length ? " · " + missing.length + " unproduced" : "") +
        (owners.length ? " · " + owners.join(" / ") : "");
      // The hover names the event types themselves. "partial" alone tells an
      // operator something is missing without saying what, which is how a
      // declared-but-unproducible event type stayed invisible on the one strip
      // whose job is to state coverage rather than imply it.
      chip.title = (missing.length ? "no producer: " + missing.join(", ") + " — " : "") +
        String(entry.note || "");
      strip.appendChild(chip);
    }
  }

  function renderLogSummary() {
    const summary = $("logs-summary");
    if (!summary) return;
    const unreadable = LOG_CLASSES.filter((cls) => !Array.isArray(logBuffers[cls]));
    if (unreadable.length) {
      summary.textContent = unreadable.join(" + ") + " log store unreadable — see the class cell";
      summary.dataset.tone = "error";
      return;
    }
    const counts = LOG_CLASSES.map((cls) => logBuffers[cls].filter(passesSeverity).length);
    summary.textContent = counts[0] + " system · " + counts[1] + " strategy record" +
      (counts[1] === 1 ? "" : "s") + " shown from separate stores" +
      (logMinSeverity ? " (min " + logMinSeverity + ")" : "");
    summary.dataset.tone = "ok";
  }

  function renderLogs(snap) {
    if (snap && snap.ok === false && !snap.classes) {
      logsUnavailable(String(snap.error || "unknown"));
      return;
    }
    const classes = (snap && snap.classes) || {};
    for (const cls of LOG_CLASSES) renderLogClassCell(cls, classes[cls]);
    renderLogCoverage(snap && snap.source_coverage);
    const note = $("logs-note");
    if (note) note.textContent = String((snap && snap.coverage_note) || "");
    renderLogSummary();
    const healthy = LOG_CLASSES.every((cls) => Array.isArray(logBuffers[cls]));
    setLogsDot(healthy ? "fresh" : "stale", healthy ? "log stores readable" : "a log store is unreadable");
  }

  function logsUnavailable(reason) {
    for (const cls of LOG_CLASSES) {
      logBuffers[cls] = null;
      setLogClassMessage(cls, "log pane unavailable: " + reason, "error");
      const count = $("logs-" + cls + "-count");
      if (count) { count.textContent = "unreadable"; count.dataset.tone = "error"; }
    }
    const summary = $("logs-summary");
    if (summary) { summary.textContent = "log pane unavailable: " + reason; summary.dataset.tone = "error"; }
    setLogsDot("stale", "log endpoint failing");
  }

  function logsNotMounted() {
    for (const cls of LOG_CLASSES) {
      logBuffers[cls] = null;
      setLogClassMessage(cls, "log pane not mounted — SRS-LOG-001 provider not composed on this runtime (ATP_LOG_DIR unset)", "warn");
      const count = $("logs-" + cls + "-count");
      if (count) { count.textContent = "—"; delete count.dataset.tone; }
    }
    const summary = $("logs-summary");
    if (summary) {
      summary.textContent = "log pane not mounted — SRS-LOG-001 provider not composed on this runtime";
      summary.dataset.tone = "warn";
    }
    const strip = $("logs-coverage");
    if (strip) strip.textContent = "";
    setLogsDot("wait", "logs route not mounted");
  }

  // A live LOGS event prepends to the class buffer it belongs to. Only applied
  // once a poll has established a readable buffer: appending to an unreadable
  // trail would paint a partial log as if it were the trail.
  function onLogEvent(record) {
    if (!record || typeof record !== "object") return;
    const cls = String(record.log_class || "");
    if (!LOG_CLASSES.includes(cls) || !Array.isArray(logBuffers[cls])) return;
    // The REST poll and this channel both carry the same records, so a record
    // the last snapshot already delivered arrives here too. Rendering it again
    // would show one audit event as two — on the surface whose whole job is to
    // say what happened, a duplicate reads as a second occurrence. renderLogs
    // already dedupes its side of the merge; this is the other side of it.
    const key = logEventKey(record);
    if (logBuffers[cls].some((seen) => logEventKey(seen) === key)) return;
    logBuffers[cls].unshift(record);
    logLiveEvents[cls].unshift(record);
    if (logLiveEvents[cls].length > LOG_LIVE_EVENT_CAP) {
      logLiveEvents[cls].length = LOG_LIVE_EVENT_CAP;
    }
    renderLogClass(cls);
    renderLogSummary();
  }

  async function pollLogs() {
    try {
      const res = await fetch(LOGS_ROUTE, {
        cache: "no-store",
        signal: AbortSignal.timeout(POLL_MS),
      });
      if (res.ok) {
        renderLogs(await res.json());
      } else if (res.status === 404) {
        logsNotMounted();
      } else {
        logsUnavailable("HTTP " + res.status);
      }
    } catch (_e) {
      logsUnavailable("endpoint unreachable");
    }
    setTimeout(pollLogs, POLL_MS);
  }

  // ----- boot ------------------------------------------------------------ //
  buildAll();
  initLogs();
  initBacktest();
  initReservoir();
  initResearch();
  initNavigation();
  initKillSwitch();
  initHotSwap();
  connect();
  poll();
  pollStrategies();
  pollBacktests();
  pollAccount();
  pollReservoir();
  pollResearch();
  pollNavigation();
  pollAlerts();
  pollKillSwitch();
  pollHotSwap();
  pollLogs();
})();
