"""L1 unit — the UI-5 Hot-Swap cell + cool-down helpers ship fail-closed.

Exercises the exact browser helpers (``python/atp_dashboard/assets/hotswap.js``)
via node, so:

* a cell whose ``data_source`` is ``deferred:*`` is UNKNOWN — its value is never
  read, so a deferred producer can never talk the pane into a resolved fact;
* the cool-down classifier tri-states the SYS-49e dial (deferred / expired /
  active) and never renders an unreadable expiry as a known countdown.

Time is injected (``nowMs``) so the classification is deterministic. Skips where
node is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HOTSWAP_JS = (
    Path(__file__).resolve().parents[2] / "python" / "atp_dashboard" / "assets" / "hotswap.js"
)
_NODE = shutil.which("node")

_DAY_MS = 86400000
_NOW_MS = 1_767_225_600_000  # a fixed epoch-ms instant (deterministic, no clock read)


def _eval(expr: str) -> str:
    script = (
        f"const h = require({json.dumps(str(HOTSWAP_JS))});process.stdout.write(String({expr}));"
    )
    result = subprocess.run([str(_NODE), "-e", script], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.mark.skipif(_NODE is None, reason="node unavailable to exercise the browser helpers")
def test_a_deferred_cell_is_never_read_as_a_value() -> None:
    # A deferred producer must never resolve a fact: value is ignored entirely.
    assert _eval('h.hotSwapCellValue({value:"x",data_source:"deferred:SRS-RESV-002"})') == "null"
    assert _eval('h.hotSwapCellBool({value:true,data_source:"deferred:SRS-RESV-004"})') == "null"
    # A live cell resolves.
    assert _eval('h.hotSwapCellValue({value:"alpha-1",data_source:"hot_swap_state"})') == "alpha-1"
    assert _eval('h.hotSwapCellBool({value:true,data_source:"hot_swap_state"})') == "true"
    assert _eval('h.hotSwapCellBool({value:false,data_source:"hot_swap_state"})') == "false"


@pytest.mark.skipif(_NODE is None, reason="node unavailable to exercise the browser helpers")
def test_malformed_cells_are_unknown_not_coerced() -> None:
    # A non-bool live value is NOT a bool (tri-state stays null), and a
    # malformed cell is unknown — never coerced to a truthy fact.
    assert _eval('h.hotSwapCellBool({value:"true",data_source:"hot_swap_state"})') == "null"
    assert _eval("h.hotSwapCellValue(null)") == "null"
    assert _eval("h.hotSwapCellValue({value:1})") == "null"  # no data_source => unknown


@pytest.mark.skipif(_NODE is None, reason="node unavailable to exercise the browser helpers")
def test_cooldown_is_deferred_when_expiry_is_unknown() -> None:
    # No readable expiry => deferred dial (dashed readout), never a fabricated
    # countdown.
    state = json.loads(_eval(f"JSON.stringify(h.hotSwapCooldown(null, {_NOW_MS}, 7))"))
    assert state["state"] == "deferred"
    assert state["label"] == "— —"


@pytest.mark.skipif(_NODE is None, reason="node unavailable to exercise the browser helpers")
def test_cooldown_expired_reads_ready() -> None:
    # SRS-RESV-006 changed the 4th argument from absent to load-bearing: the SERVER's
    # in_effect is the authority for the state. `false` is what a real snapshot carries
    # alongside a past expiry, and it still reads READY.
    past = _NOW_MS - _DAY_MS
    expiry = f'"{_iso(past)}"'
    state = json.loads(_eval(f"JSON.stringify(h.hotSwapCooldown({expiry}, {_NOW_MS}, 7, false))"))
    assert state["state"] == "expired"
    assert state["label"] == "READY"


@pytest.mark.skipif(_NODE is None, reason="node unavailable to exercise the browser helpers")
def test_cooldown_active_reports_remaining_and_fraction() -> None:
    future = _NOW_MS + 3 * _DAY_MS + 4 * 3600000  # 3d 04h remaining
    expiry = f'"{_iso(future)}"'
    state = json.loads(_eval(f"JSON.stringify(h.hotSwapCooldown({expiry}, {_NOW_MS}, 7, true))"))
    assert state["state"] == "active"
    assert state["label"] == "3d 04h"
    # 3d04h of a 7-day window remaining.
    assert 0.4 < state["fraction"] < 0.5


# --------------------------------------------------------------------------- #
# SRS-RESV-006 — the server's in_effect outranks the browser clock
# --------------------------------------------------------------------------- #
#
# The window is classified server-side, against the server clock and the durable record.
# Letting the pane re-decide it locally makes the VIEWER's clock a second source of truth
# about whether a live-strategy swap is safe — and this state gates the promote control.


@pytest.mark.skipif(_NODE is None, reason="node unavailable to exercise the browser helpers")
def test_a_fast_browser_clock_cannot_retire_a_window_the_server_says_is_open() -> None:
    # Expiry already passed by the viewer's clock, but the server says in_effect. The dial
    # must stay ACTIVE: a few minutes of clock skew must not render READY over a window the
    # server is still suppressing on.
    past = _NOW_MS - 60_000
    expiry = f'"{_iso(past)}"'
    state = json.loads(_eval(f"JSON.stringify(h.hotSwapCooldown({expiry}, {_NOW_MS}, 7, true))"))
    assert state["state"] == "active"
    assert state["fraction"] == 0
    assert state["label"] == "< 1m", "a skewed clock must not render a negative countdown"


@pytest.mark.skipif(_NODE is None, reason="node unavailable to exercise the browser helpers")
def test_a_slow_browser_clock_cannot_show_a_countdown_the_server_says_is_over() -> None:
    # The mirror image: the viewer's clock still shows time remaining, but the server says
    # the window is closed.
    future = _NOW_MS + 2 * _DAY_MS
    expiry = f'"{_iso(future)}"'
    state = json.loads(_eval(f"JSON.stringify(h.hotSwapCooldown({expiry}, {_NOW_MS}, 7, false))"))
    assert state["state"] == "expired"
    assert state["label"] == "READY"


@pytest.mark.skipif(_NODE is None, reason="node unavailable to exercise the browser helpers")
def test_an_unknown_in_effect_never_reads_ready_on_the_browser_clock_alone() -> None:
    # The fail-closed case. The expiry has passed locally but the server did not say whether
    # the window is in effect, so the pane cannot confirm it closed — it stays deferred, and
    # hotActionable() holds the promote control inert on exactly that.
    past = _NOW_MS - _DAY_MS
    expiry = f'"{_iso(past)}"'
    state = json.loads(_eval(f"JSON.stringify(h.hotSwapCooldown({expiry}, {_NOW_MS}, 7, null))"))
    assert state["state"] == "deferred"
    assert state["label"] == "— —"


@pytest.mark.skipif(_NODE is None, reason="node unavailable to exercise the browser helpers")
def test_a_never_swapped_snapshot_reads_ready_without_any_expiry() -> None:
    # NEVER_SWAPPED carries in_effect:false and NO timestamps — there is no window to date.
    # That must render READY, not a hatched unknown: no swap has ever happened, so nothing
    # is being suppressed.
    state = json.loads(_eval(f"JSON.stringify(h.hotSwapCooldown(null, {_NOW_MS}, 7, false))"))
    assert state["state"] == "expired"
    assert state["label"] == "READY"


def _iso(epoch_ms: int) -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_ms / 1000))
