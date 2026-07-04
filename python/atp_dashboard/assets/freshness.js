/* Pure freshness classifier for the SRS-UI-001 dashboard.
 *
 * A required channel is reported "fresh" (healthy) ONLY while it refreshes
 * within its budget — the NFR-P2 5-second contract for the METRICS/benchmark
 * panel. Jitter tolerance lives in a SEPARATE "warn" state, so a channel that
 * is over its budget is NEVER shown as healthy (the SRS-UI-001 refresh SLA must
 * fail at the contract boundary, not budget+grace).
 *
 * Loadable in the browser (attaches `freshnessState` to window) and in node
 * (`module.exports`) so the classifier is unit-tested against the exact code
 * that ships. All arguments are milliseconds.
 *
 *   freshnessState(4999, 5000, 1500) === "fresh"   // within budget
 *   freshnessState(5001, 5000, 1500) === "warn"    // over budget -> NOT fresh
 *   freshnessState(6501, 5000, 1500) === "stale"   // past the grace window
 *   freshnessState(null,  5000, 1500) === "wait"   // never seen
 */
(function (root) {
  "use strict";

  function freshnessState(staleness, budget, grace) {
    if (staleness === null || staleness === undefined || !isFinite(staleness)) {
      return "wait";
    }
    if (staleness <= budget) return "fresh"; // within the refresh contract
    if (staleness <= budget + (grace || 0)) return "warn"; // over budget, within jitter grace
    return "stale"; // clearly violating the refresh contract
  }

  root.freshnessState = freshnessState;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { freshnessState };
  }
})(typeof window !== "undefined" ? window : globalThis);
