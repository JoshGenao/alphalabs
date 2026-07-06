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
    for (const panel of ["pnl", "metrics", "health", "latency"]) addFreshDot(panel);
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
      ws.send(JSON.stringify({ type: "SUBSCRIBE", channels: ["PNL", "METRICS", "HEARTBEAT"] }));
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

  function onEvent(channel, data) {
    if (channel === "PNL") {
      applyMeta("body-pnl", data.strategy_id);
      for (const [k, , kind] of ROWS.pnl) setField("body-pnl", k, data[k], kind);
    } else if (channel === "METRICS") {
      applyMeta("body-metrics", data.strategy_id);
      for (const [k, , kind] of ROWS.metrics) setField("body-metrics", k, data[k], kind);
    } else if (channel === "HEARTBEAT") {
      for (const [k, , kind] of ROWS.health) setField("body-health", k, data[k], kind);
    }
    noteActivity(channel);
  }

  function applyMeta(bodyId, strategyId) {
    const row = $(bodyId).querySelector('[data-meta="strategy"] .metric__value');
    if (!row) return;
    if (strategyId) { row.textContent = strategyId; row.classList.remove("is-deferred"); }
    else { row.textContent = "none"; row.classList.add("is-deferred"); }
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
    for (const [, text] of items.slice(0, 4)) {
      const li = document.createElement("li"); li.textContent = String(text); notes.appendChild(li);
    }
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

  // ----- boot ------------------------------------------------------------ //
  buildAll();
  connect();
  poll();
})();
