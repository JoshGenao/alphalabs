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
 *   hotSwapCooldown(null, now, 7).state    === "deferred"
 *   hotSwapCooldown(past, now, 7).state    === "expired"
 *   hotSwapCooldown(future, now, 7).state  === "active"
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

  function hotSwapCooldown(expiresAtIso, nowMs, cooldownDays) {
    var expires = typeof expiresAtIso === "string" ? Date.parse(expiresAtIso) : NaN;
    if (!isFinite(expires) || typeof nowMs !== "number" || !isFinite(nowMs)) {
      return { state: "deferred", fraction: 0, label: "— —" };
    }
    var remainMs = expires - nowMs;
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
