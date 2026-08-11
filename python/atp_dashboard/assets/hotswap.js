/* Pure Hot-Swap cell + cool-down helpers for the UI-5 Changeover Console.
 *
 * Fail-closed readers over the dashboard's {value, data_source} cells and a
 * cool-down classifier for the SYS-49e dial. Kept side-effect-free and time is
 * injected (nowMs), so the exact code that ships is unit-tested under node.
 *
 * A cell whose data_source begins "deferred:" is UNKNOWN — its value is never
 * read, so a deferred producer can never talk the pane into a resolved fact.
 *
 *   hotSwapCellValue({value:"alpha-1",data_source:"hot_swap_state"}) === "alpha-1"
 *   hotSwapCellValue({value:"x",data_source:"deferred:SRS-RESV-002"}) === null
 *   hotSwapCellBool({value:true,data_source:"hot_swap_state"})        === true
 *   hotSwapCellBool({value:true,data_source:"deferred:SRS-RESV-004"}) === null
 *   hotSwapCooldown(null, now, 7, null)    .state === "deferred"  // server said nothing
 *   hotSwapCooldown(past, now, 7, false)   .state === "expired"
 *   hotSwapCooldown(future, now, 7, true)  .state === "active"
 *   hotSwapCooldown(past, now, 7, true)    .state === "active"    // server outranks the clock
 */
(function (root) {
  "use strict";

  function hotSwapCellValue(cell) {
    if (!cell || typeof cell !== "object") return null;
    if (typeof cell.data_source !== "string") return null;
    if (cell.data_source.indexOf("deferred:") === 0) return null;
    return cell.value === undefined ? null : cell.value;
  }

  function hotSwapCellBool(cell) {
    var v = hotSwapCellValue(cell);
    return v === true ? true : v === false ? false : null;
  }

  function _pad2(n) {
    return String(n).padStart(2, "0");
  }

  // `inEffect` is the SERVER's answer (SRS-RESV-006 classifies the window against the
  // server clock and the durable record) and it is the AUTHORITY for `state` whenever it is
  // known. The arithmetic below is kept only for `fraction` and `label`, which are cosmetic.
  //
  // Without that split the browser clock becomes a second source of truth: a viewer whose
  // machine is a few minutes fast would render READY on a window the server is still
  // suppressing, and the promote control keys off this state. Tri-state on purpose —
  // `null` means the server did not say, which is never "ready".
  function hotSwapCooldown(expiresAtIso, nowMs, cooldownDays, inEffect) {
    var expires = typeof expiresAtIso === "string" ? Date.parse(expiresAtIso) : NaN;
    if (!isFinite(expires) || typeof nowMs !== "number" || !isFinite(nowMs)) {
      // No parseable expiry. An explicit not-in-effect is still a real answer — a window
      // that has never existed has no expiry to report — so it renders READY, not unknown.
      if (inEffect === false) return { state: "expired", fraction: 0, label: "READY" };
      return { state: "deferred", fraction: 0, label: "— —" };
    }
    var remainMs = expires - nowMs;
    if (inEffect === true) {
      // The server says the window is OPEN. Trust that over local arithmetic, and clamp the
      // cosmetic remainder so a skewed clock cannot render a negative countdown.
      if (!(remainMs > 0)) return { state: "active", fraction: 0, label: "< 1m" };
    } else if (inEffect === false) {
      return { state: "expired", fraction: 0, label: "READY" };
    } else if (!(remainMs > 0)) {
      // in_effect unknown AND the expiry has passed: the pane cannot confirm the window
      // closed, so it stays deferred rather than claiming READY on its own clock.
      return { state: "deferred", fraction: 0, label: "— —" };
    }
    if (!(remainMs > 0)) return { state: "expired", fraction: 0, label: "READY" };
    var days = cooldownDays > 0 ? cooldownDays : 7;
    var totalMs = days * 86400000;
    var fraction = Math.max(0, Math.min(1, remainMs / totalMs));
    var d = Math.floor(remainMs / 86400000);
    var h = Math.floor((remainMs % 86400000) / 3600000);
    var m = Math.floor((remainMs % 3600000) / 60000);
    var label = d > 0 ? d + "d " + _pad2(h) + "h" : h > 0 ? h + "h " + _pad2(m) + "m" : m + "m";
    return { state: "active", fraction: fraction, label: label };
  }

  root.hotSwapCellValue = hotSwapCellValue;
  root.hotSwapCellBool = hotSwapCellBool;
  root.hotSwapCooldown = hotSwapCooldown;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { hotSwapCellValue: hotSwapCellValue, hotSwapCellBool: hotSwapCellBool, hotSwapCooldown: hotSwapCooldown };
  }
})(typeof window !== "undefined" ? window : globalThis);
