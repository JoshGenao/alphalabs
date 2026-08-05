"""L4 boundary test: the log pane's REST-poll / WebSocket interleaving, in JS.

The pane has two feeds for one buffer — a periodic ``/dashboard/api/logs``
snapshot and live ``LOGS`` channel events — and they race. A poll that STARTED
before an event can resolve AFTER it, and a render that replaced the buffer with
that older snapshot would erase the event from the pane until some later poll
happened to include it. An audit event that appears and then vanishes is worse
than one that arrives late.

Asserting that in Python would only ever check the source text. This runs the
real ``app.js`` render logic under node with a minimal DOM stub, drives the
interleaving explicitly, and asserts the event survives — the behaviour, not the
spelling. Skipped (not failed) where node is unavailable, so the suite still runs
on a machine without it; the browser-level proof is
``tests/e2e/test_dashboard_logs.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "python" / "atp_dashboard" / "assets" / "app.js"

pytestmark = pytest.mark.boundary

_NODE = shutil.which("node")


def _event(
    message: str,
    correlation_id: str,
    log_class: str = "strategy",
    record_id: str = "1-1-1",
    timestamp: str = "2026-08-05T00:00:00.000+00:00",
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "timestamp": timestamp,
        "severity": "INFO",
        "source": "strategy" if log_class == "strategy" else "kill_switch",
        "event_type": "SIGNAL",
        "message": message,
        "correlation_id": correlation_id,
        "log_class": log_class,
        "strategy_id": "sma-1" if log_class == "strategy" else None,
    }


#: A DOM stub thin enough to be obviously faithful: the pane's render path only
#: reads/writes textContent, hidden, dataset and appends rows.
_HARNESS = """
const fs = require("fs");

function makeNode() {
  const node = {
    textContent: "", innerHTML: "", hidden: false, disabled: false, value: "",
    dataset: {}, className: "", style: {}, children: [], firstChild: null,
    title: "", id: "",
    appendChild(child) { this.children.push(child); return child; },
    append(...kids) { for (const kid of kids) this.children.push(kid); },
    insertBefore(child) { this.children.unshift(child); return child; },
    removeChild(child) { return child; },
    remove() {},
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    addEventListener() {}, removeEventListener() {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    closest: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    focus() {},
  };
  return node;
}
const nodes = {};
function byId(id) { return (nodes[id] = nodes[id] || makeNode()); }

global.document = {
  getElementById: byId,
  createElement: () => makeNode(),
  createElementNS: () => makeNode(),
  createTextNode: () => makeNode(),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  body: makeNode(),
};
global.window = {
  location: { protocol: "http:", host: "localhost", href: "" },
  addEventListener() {}, removeEventListener() {},
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  requestAnimationFrame: () => 0,
};
global.location = global.window.location;
global.performance = { now: () => 0 };
global.WebSocket = function () { this.send = () => {}; this.close = () => {}; };
global.fetch = () => new Promise(() => {});          // polls never resolve on their own
global.setTimeout = () => 0;                          // no background loops
global.setInterval = () => 0;
global.AbortSignal = { timeout: () => null };

// Load app.js and capture the functions under test. The file is an IIFE, so it
// is evaluated with a tail that exports exactly what this test drives.
const source = fs.readFileSync(process.argv[2], "utf8");
const exposed = source.replace(
  /\\n\\}\\)\\(\\);?\\s*$/,
  "\\n  global.__logs = { renderLogs, onLogEvent, logBuffers };\\n})();\\n"
);
eval(exposed);

const [snapshotA, liveEvent, snapshotStale] = JSON.parse(process.argv[3]);
const api = global.__logs;

const rendered = () => (api.logBuffers.strategy || []).map((r) => r.message);

// 1. First poll lands.
api.renderLogs(snapshotA);
// 2. A live LOGS event arrives.
api.onLogEvent(liveEvent);
// What the operator is looking at RIGHT NOW — for up to a whole poll interval.
// Reporting only the post-step-3 buffer would hide any fault the next poll
// happens to repair, and "wrong for four seconds" is still wrong on an audit
// surface.
const afterEvent = rendered();
// 3. A poll that STARTED BEFORE the event resolves now, without it.
api.renderLogs(snapshotStale);

process.stdout.write(
  process.argv[4] === "after-event"
    ? JSON.stringify(afterEvent)
    : JSON.stringify(rendered())
);
"""


def _snapshot(records: list[dict[str, object]]) -> dict[str, object]:
    def cell(log_class: str, rows: list[dict[str, object]]) -> dict[str, object]:
        return {
            "ok": True,
            "log_class": log_class,
            "records": rows,
            "matched": len(rows),
            "truncated": False,
            "store": f"{log_class}.jsonl",
            "store_present": True,
            "error": None,
        }

    return {
        "generated_at": "2026-08-05T00:00:00Z",
        "ok": True,
        "srs_ref": "SRS-LOG-001",
        "classes": {"system": cell("system", []), "strategy": cell("strategy", records)},
        "event_fields": list(_event("x", "y")),
        "severities": ["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"],
        "source_coverage": [],
        "coverage_note": "",
    }


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_stale_poll_cannot_erase_a_live_log_event(tmp_path: Path) -> None:
    """The failure this guards: an audit event that appears, then disappears."""

    harness = tmp_path / "harness.js"
    harness.write_text(textwrap.dedent(_HARNESS), encoding="utf-8")

    persisted = _event("already persisted", "run-0", record_id="1-1-100")
    live = _event("arrived over the channel", "run-1", record_id="1-1-200")
    payload = json.dumps([_snapshot([persisted]), live, _snapshot([persisted])])

    result = subprocess.run(
        [str(_NODE), str(harness), str(APP_JS), payload],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    messages = json.loads(result.stdout)

    assert "arrived over the channel" in messages, (
        "a stale poll erased a live LOGS event from the pane — an audit event that "
        "appears and then vanishes"
    )
    assert messages.count("arrived over the channel") == 1, "the live event was duplicated"
    assert "already persisted" in messages


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_snapshot_that_contains_the_event_stops_overlaying_it(tmp_path: Path) -> None:
    """Once the trail carries the event, it is history — not a pending overlay.

    Otherwise the hold-back list would grow forever and every later snapshot
    would render the same event twice.
    """

    harness = tmp_path / "harness.js"
    harness.write_text(textwrap.dedent(_HARNESS), encoding="utf-8")

    persisted = _event("already persisted", "run-0", record_id="1-1-100")
    live = _event("arrived over the channel", "run-1", record_id="1-1-200")
    # The third snapshot is a FRESH one that now includes the live event.
    payload = json.dumps([_snapshot([persisted]), live, _snapshot([live, persisted])])

    result = subprocess.run(
        [str(_NODE), str(harness), str(APP_JS), payload],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    messages = json.loads(result.stdout)

    assert messages.count("arrived over the channel") == 1, (
        "the event is both overlaid and in the snapshot — it renders twice"
    )
    assert messages == ["arrived over the channel", "already persisted"]


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_an_event_the_snapshot_already_delivered_is_not_rendered_twice(
    tmp_path: Path,
) -> None:
    """The REST poll and the channel carry the SAME records — order reversed.

    The sibling cases drive event-then-poll. This is poll-then-event: the
    snapshot renders a record, and the LOGS event for that same record arrives
    afterwards. Nothing was wrong with the merge on the snapshot side; the live
    side simply prepended whatever it was handed. On a surface whose job is to
    say what happened, one audit event rendered twice reads as two occurrences.
    """

    harness = tmp_path / "harness.js"
    harness.write_text(textwrap.dedent(_HARNESS), encoding="utf-8")

    already_polled = _event("already persisted", "run-0", record_id="1-1-100")
    # The SAME record, arriving over the channel after the poll delivered it.
    payload = json.dumps([_snapshot([already_polled]), already_polled, _snapshot([already_polled])])

    result = subprocess.run(
        [str(_NODE), str(harness), str(APP_JS), payload, "after-event"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    messages = json.loads(result.stdout)

    assert messages.count("already persisted") == 1, (
        "a record the snapshot already delivered was rendered a second time when its "
        "LOGS event arrived — one audit event shown as two"
    )


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_held_back_event_is_not_pinned_above_newer_records(tmp_path: Path) -> None:
    """Newest-first means by TIMESTAMP, not by which path delivered it.

    A live event is held back until a snapshot contains it. Once a burst exceeds
    the pane's page, that event falls off the newest-N window and no later
    snapshot can ever contain it again — so concatenating held-back events in
    front would pin it above strictly newer records permanently, in a table
    labelled newest-first.
    """

    harness = tmp_path / "harness.js"
    harness.write_text(textwrap.dedent(_HARNESS), encoding="utf-8")

    older_live = _event(
        "older live event",
        "run-1",
        record_id="1-1-100",
        timestamp="2026-08-05T00:00:00.000+00:00",
    )
    newer = _event(
        "newer persisted",
        "run-2",
        record_id="1-1-200",
        timestamp="2026-08-05T00:00:09.000+00:00",
    )
    # The final snapshot has moved past the live event (it fell off the page).
    payload = json.dumps([_snapshot([]), older_live, _snapshot([newer])])

    result = subprocess.run(
        [str(_NODE), str(harness), str(APP_JS), payload],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    messages = json.loads(result.stdout)

    assert messages == ["newer persisted", "older live event"], (
        "a held-back live event was pinned above a strictly newer record in a "
        f"newest-first table: {messages}"
    )


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_two_identical_looking_records_are_not_deduped_into_one(tmp_path: Path) -> None:
    """The overlay keys on record_id, not on values that legitimately repeat.

    A retried operation writes the same message with the same correlation id, and
    the rendered timestamp is only milliseconds — so a value-keyed merge would
    see the live event as "already in the snapshot" and drop a REAL second audit
    event from the pane.
    """

    harness = tmp_path / "harness.js"
    harness.write_text(textwrap.dedent(_HARNESS), encoding="utf-8")

    # Identical in every rendered field EXCEPT the identity.
    first = _event("retry me", "dup-1", record_id="1-1-100")
    second = _event("retry me", "dup-1", record_id="1-1-200")
    # The stale snapshot carries only the FIRST one.
    payload = json.dumps([_snapshot([first]), second, _snapshot([first])])

    result = subprocess.run(
        [str(_NODE), str(harness), str(APP_JS), payload],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    messages = json.loads(result.stdout)

    assert messages.count("retry me") == 2, (
        "two distinct audit records collapsed into one — the merge keyed on "
        "values instead of identity"
    )


def test_node_harness_is_actually_exercised() -> None:
    """Guard the guard: if node vanishes from CI, say so rather than silently skip."""

    if _NODE is None:  # pragma: no cover - environment-dependent
        pytest.skip("node is not installed")
    assert APP_JS.exists()
    assert "logLiveEvents" in APP_JS.read_text(encoding="utf-8"), (
        "the pane no longer holds live events back from snapshot replacement"
    )
    assert sys.version_info >= (3, 12)
