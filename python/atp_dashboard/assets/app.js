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
    for (const panel of ["pnl", "metrics", "health", "latency", "strategies", "backtest", "account", "reservoir", "research", "alerts"]) addFreshDot(panel);
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
      ws.send(JSON.stringify({ type: "SUBSCRIBE", channels: ["PNL", "METRICS", "HEARTBEAT", "STRATEGY_STATE", "ACCOUNT_STATUS", "RESERVOIR_RANKING"] }));
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
  let inventoryGen = 0;
  let inventoryExpected = null;
  let inventorySeen = 0;

  function removeInventoryRow(tr) {
    if (promoteArmedId === tr.dataset.strategy) disarmPromote(true);
    tr.remove();
  }

  function sweepStaleInventoryRows() {
    const rows = $("inventory-rows");
    if (!rows) return;
    rows.querySelectorAll("tr").forEach((tr) => {
      if (tr.dataset.gen !== String(inventoryGen)) removeInventoryRow(tr);
    });
    if (!rows.children.length) $("inventory-table").hidden = true;
  }

  function clearInventoryRows() {
    const rows = $("inventory-rows");
    if (!rows) return;
    rows.querySelectorAll("tr").forEach(removeInventoryRow);
    $("inventory-table").hidden = true;
  }

  function renderInventoryRow(data) {
    const rows = $("inventory-rows");
    if (!rows) return;
    const key = String(data.strategy_id);
    // A data refresh voids any staged confirmation for this row — an armed
    // button must never survive a row rebuild and fire against renewed data —
    // and the designation readout returns to resting (a stale "armed" caption
    // with nothing staged would misstate the control's state).
    if (promoteArmedId === key) disarmPromote(true);
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
    const cd = el("span", "manage__cd"); cd.setAttribute("aria-hidden", "true");
    manage.append(btn, cd);
    tr.appendChild(manage);
    tr.dataset.gen = String(inventoryGen);
    $("inventory-table").hidden = false;
    inventorySeen += 1;
    if (inventoryExpected !== null && inventorySeen >= inventoryExpected) {
      sweepStaleInventoryRows();
    }
  }

  function onInventoryEvent(data) {
    const summary = $("inventory-summary");
    if (data.event === "inventory-summary") {
      if (!summary) return;
      const n = Number(data.strategy_count);
      if (data.ok === false || data.ok !== true || !Number.isInteger(n) || n < 0) {
        // Unknown truth — an unreadable source (ok:false) and a malformed or
        // version-skewed summary (ok not exactly true, or a count that is not
        // a non-negative integer) both fail closed: clear the rows too; stale
        // actionable PROMOTE LIVE rows must not survive an error caption.
        clearInventoryRows();
        inventoryExpected = null;
        summary.textContent = "inventory unavailable: " +
          (data.ok === false ? String(data.error || "unknown") : "malformed summary");
        summary.dataset.tone = "error";
      } else {
        inventoryGen += 1;
        inventoryExpected = n;
        inventorySeen = 0;
        if (n === 0) clearInventoryRows();
        summary.textContent = n === 0
          ? "no strategies deployed"
          : n + " strateg" + (n === 1 ? "y" : "ies") + " · deployed version live · other cells await their producer features";
        summary.dataset.tone = "ok";
      }
      return;
    }
    if (data.strategy_id) renderInventoryRow(data);
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
    btn.disabled = false;
  }

  (function bindPromoteControls() {
    const rows = $("inventory-rows");
    if (!rows) return;
    rows.addEventListener("click", (event) => {
      const btn = event.target.closest(".manage__btn");
      if (!btn || btn.disabled) return;
      const id = String(btn.dataset.strategy || "");
      if (!id) return;
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
      return;
    }
    if (snap.upstream_reachable) {
      status.textContent = "research environment reachable (HTTP " + snap.status_code + ") at " + snap.prefix;
      status.dataset.tone = "ok";
      open.disabled = false;
      open.dataset.embedPath = snap.embed_path || "";
    } else {
      status.textContent = snap.detail || "research upstream unreachable";
      status.dataset.tone = "err";
      open.disabled = true;
    }
  }

  function initResearch() {
    const open = $("research-open");
    const frame = $("research-frame");
    if (!open || !frame) return;
    open.addEventListener("click", () => {
      const path = open.dataset.embedPath;
      if (!path) return;
      // Lazy same-origin load: the iframe src is only ever the probe-provided
      // /research/… path on THIS origin — never an external URL (SEC-002).
      if (!researchFrameLoaded || frame.getAttribute("src") !== path) {
        frame.src = path;
        researchFrameLoaded = true;
      }
      frame.hidden = false;
      open.textContent = "Reload research environment";
    });
  }

  async function pollResearch() {
    try {
      const res = await fetch(RESEARCH_ROUTE, { cache: "no-store" });
      if (res.ok) {
        lastChannelAt["RESEARCH"] = performance.now();
        renderResearch(await res.json());
      } else if (res.status === 404) {
        const s = $("research-status");
        if (s) { s.textContent = "research not mounted — SRS-RES-001 provider not composed on this runtime"; s.dataset.tone = "warn"; }
      }
    } catch (_e) { /* transient; next tick retries */ }
    setTimeout(pollResearch, POLL_MS);
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
    inventoryExpected = null;
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
          });
          for (const row of snap.strategies) renderInventoryRow(row);
        } else if (snap.ok === false) {
          onInventoryEvent({ event: "inventory-summary", ok: false, error: snap.error });
        } else {
          inventoryUnavailable("inventory unavailable: malformed snapshot");
        }
      } else if (res.status === 404) {
        // Route disappearance: the provider is no longer composed on this
        // runtime — the caption alone is not enough, the rows go too.
        clearInventoryRows();
        inventoryExpected = null;
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

  // ----- kill switch (SRS-SAFE-001 minimal affordance; rich control = UI-4) //
  // Two-step arm-then-fire against the CONTRACT route on this same runtime.
  // The rendered outcome is the runtime's own response, verbatim — a refusal
  // (428/500/501/504) is shown as its error type, never dressed as success.
  const KILL_SWITCH_ROUTE = "/api/v1/kill-switch?confirm=true";
  const KILL_ARM_WINDOW_MS = 5000;
  let killArmTimer = null;

  function killStatus(text, tone) {
    const el = $("killswitch-status");
    el.textContent = text;
    el.dataset.tone = tone;
  }

  function disarmKillSwitch() {
    const btn = $("killswitch-btn");
    btn.dataset.armed = "false";
    btn.textContent = "KILL SWITCH";
    if (killArmTimer) { clearTimeout(killArmTimer); killArmTimer = null; }
  }

  async function fireKillSwitch() {
    const btn = $("killswitch-btn");
    btn.disabled = true;
    killStatus("activating…", "pending");
    try {
      const res = await fetch(KILL_SWITCH_ROUTE, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      });
      const body = await res.json();
      if (res.ok) {
        // A 200 means the sequence RAN — not that every phase succeeded.
        // Count FAILED per-order outcomes so a partial failure is loudly
        // distinguishable from a clean liquidation (the rich per-phase
        // status control is UI-4; this affordance must still never dress a
        // failure as success).
        const countFailed = (entries) => Array.isArray(entries)
          ? entries.filter((e) => e && e.outcome && e.outcome.status === "FAILED").length
          : 0;
        const failed = countFailed(body.liquidation_orders) + countFailed(body.cancelled_orders);
        const disconnected = body.ib_gateway_disconnected === true;
        const summary =
          "activated " + String(body.activation_id) +
          ": engines_halted=" + String(body.paper_engines_halted) +
          " liquidations=" + (Array.isArray(body.liquidation_orders) ? body.liquidation_orders.length : "?") +
          " cancels=" + (Array.isArray(body.cancelled_orders) ? body.cancelled_orders.length : "?") +
          " ib_disconnected=" + String(body.ib_gateway_disconnected);
        if (failed > 0 || !disconnected) {
          killStatus(summary + " — WITH FAILURES: " + failed + " order phase(s) FAILED" +
            (!disconnected ? ", IB NOT disconnected" : "") + " — inspect kill-switch status", "error");
        } else {
          killStatus(summary, "fired");
        }
      } else {
        const err = body && body.error ? body.error : {};
        killStatus("REFUSED " + res.status + " " + String(err.type || "UNKNOWN"), "error");
        btn.disabled = false;
      }
    } catch (error) {
      killStatus("FAILED: " + String(error), "error");
      btn.disabled = false;
    }
    disarmKillSwitch();
  }

  $("killswitch-btn").addEventListener("click", () => {
    const btn = $("killswitch-btn");
    if (btn.dataset.armed !== "true") {
      btn.dataset.armed = "true";
      btn.textContent = "CONFIRM LIQUIDATE?";
      killStatus("armed — click again within 5s to liquidate", "armed");
      killArmTimer = setTimeout(() => { disarmKillSwitch(); killStatus("", ""); }, KILL_ARM_WINDOW_MS);
      return;
    }
    fireKillSwitch();
  });

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

  // ----- boot ------------------------------------------------------------ //
  buildAll();
  initBacktest();
  initReservoir();
  initResearch();
  connect();
  poll();
  pollStrategies();
  pollBacktests();
  pollAccount();
  pollReservoir();
  pollResearch();
  pollAlerts();
})();
