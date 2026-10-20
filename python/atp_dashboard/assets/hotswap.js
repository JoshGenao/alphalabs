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
 *   hotSwapCooldownNotStarted("sw-1", "b", "NOT_STARTED", true).tone === "armed"
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

  // ONE wording for a PROMOTED swap whose OWN SYS-49e window did not start, shared by the
  // swap response and the durable confirmation that follows it, so the two cannot drift.
  //
  // `inEffect` is the store's answer for the window in force RIGHT NOW (the dial's own
  // source), tri-state. It decides the message, because "this swap's window did not start"
  // and "no window is in force" are different facts. A swap reaches NOT_STARTED with a
  // window still in force when the store KEPT a newer completion already on file, or when
  // phase two failed and phase one's provisional marker is still suppressing. The pane used
  // to say "THE COOL-DOWN IS NOT IN EFFECT: the automatic triggers are armed" in those cases
  // too, next to a dial correctly showing an active window: an alarm that contradicted the
  // instrument beside it.
  //
  //   true  -> a window IS in force: say so, and ask for the history to be reconciled.
  //   false -> nothing is suppressing the triggers: the SYS-49e fail-open, loudly.
  //   null  -> nobody can say yet: treat the triggers as armed until the dial reads ACTIVE.
  function hotSwapCooldownNotStarted(swapId, candidate, cooldownWindow, inEffect) {
    var head = "swap " + swapId + " PROMOTED " + candidate + " - but its own cool-down window is " +
      cooldownWindow + ". ";
    if (inEffect === true) {
      return {
        tone: "armed",
        text: head + "A COOL-DOWN IS STILL IN EFFECT - the window on file, shown on the dial - " +
          "so the automatic triggers stay suppressed. This swap's own window was not " +
          "confirmed: check resv006_hot_swap_cooldown_cli status to see which window is in " +
          "force, and reconcile it (SRS-RESV-006 · SYS-49e)",
      };
    }
    if (inEffect === false) {
      return {
        tone: "error",
        text: head + "THE COOL-DOWN IS NOT IN EFFECT: the automatic triggers are armed against a " +
          "strategy that just went live. Repair it with resv006_hot_swap_cooldown_cli " +
          "record-completion, or disable the automatic triggers until you have " +
          "(SRS-RESV-006 · SYS-49e)",
      };
    }
    return {
      tone: "error",
      text: head + "Whether ANY cool-down is in effect is UNKNOWN until the window is re-read: " +
        "treat the automatic triggers as armed against a strategy that just went live. If the " +
        "dial does not read ACTIVE, repair it with resv006_hot_swap_cooldown_cli " +
        "record-completion (SRS-RESV-006 · SYS-49e)",
    };
  }

  // "2026-10-20T04:51:52Z" -> "10-20 04:51Z". The year and seconds are what make a full
  // ISO instant too wide for the dial; the full value stays in the dial's title and
  // aria-label. A value this does not recognise is returned unchanged: an unexpected
  // shape is shown as it is, never guessed at.
  function _shortInstant(iso) {
    var m = /^\d{4}-(\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$/.exec(
      String(iso)
    );
    if (!m) return String(iso);
    return m[1] + " " + m[2] + (m[3] === "Z" ? "Z" : " " + m[3]);
  }

  // The dial's sub-line, in two forms. `short` is what fits INSIDE the ring: the old
  // single sentence (a full ISO instant at 9px is ~150px; the ring's inner chord where the
  // sub-line sits is ~117px) overflowed and was clipped by the ring on every state. `full`
  // is the same fact unabridged, for the dial's title and aria-label - the dial is
  // role="img", so assistive tech never read the sub-line at all.
  function hotSwapDialSub(dialState, provisional, startedIso, expiresIso) {
    if (dialState === "deferred") {
      return {
        short: "state UNKNOWN\npromotion held",
        full: "cool-down state UNKNOWN - no readable SRS-RESV-006 window on this runtime; " +
          "promotion is held",
      };
    }
    if (dialState === "expired") {
      return {
        short: "no cool-down\ntriggers may fire",
        full: "no active cool-down - automatic triggers may fire",
      };
    }
    var expires = expiresIso ? String(expiresIso) : "unknown";
    var shortLines = [];
    var full = "";
    if (provisional) {
      // A PROVISIONAL window (r13) was opened before its swap became durable and never
      // confirmed. It suppresses like a real one; the operator must be able to tell.
      shortLines.push("PROVISIONAL");
      full += "PROVISIONAL - a swap was interrupted before it could confirm this window · ";
    }
    if (startedIso) {
      shortLines.push("since " + _shortInstant(startedIso));
      full += "since " + startedIso + " · ";
    }
    shortLines.push("expires " + (expiresIso ? _shortInstant(expiresIso) : "unknown"));
    full += "expires " + expires;
    return { short: shortLines.join("\n"), full: full };
  }

  root.hotSwapCellValue = hotSwapCellValue;
  root.hotSwapCellBool = hotSwapCellBool;
  root.hotSwapCooldown = hotSwapCooldown;
  root.hotSwapCooldownNotStarted = hotSwapCooldownNotStarted;
  root.hotSwapDialSub = hotSwapDialSub;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      hotSwapCellValue: hotSwapCellValue,
      hotSwapCellBool: hotSwapCellBool,
      hotSwapCooldown: hotSwapCooldown,
      hotSwapCooldownNotStarted: hotSwapCooldownNotStarted,
      hotSwapDialSub: hotSwapDialSub,
    };
  }
})(typeof window !== "undefined" ? window : globalThis);
