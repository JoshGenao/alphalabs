"""L6 e2e — SRS-UI-001 dashboard refresh under a synthetic NFR-SC1-shaped load.

The load leg of the SRS-UI-001 acceptance evidence — the AC's "refreshes
within 5 seconds **under release baseline load**" — in its operator-authorized
**synthetic** form. A real browser session watches the live dashboard while
:class:`SyntheticStrategyLoad` drives per-strategy traffic at the NFR-SC1
strategy shape (1 live + 30 paper) through the runtime's WebSocket fan-out at
each channel's real cadence, and the browser's self-measured
worst-required-channel refresh must stay inside the NFR-P2 5-second budget —
not just at first paint, but after holding the load for multiple full METRICS
cadences.

Scope: this exercises the dashboard-facing load path (per-strategy fan-out →
WS hub → browser), NOT the fully orchestrated 1-live + 30-paper container
stack — the real strategy/market-data producers are still-deferred features
(SRS-SIM-001 / SRS-EXE-001 / SRS-MD-006/007). Re-measuring under that stack is
the deferred stronger form of this evidence and lands with those features.

Non-vacuity: the test fails unless every synthetic strategy demonstrably
published on every load channel AND the measured browser session itself
RECEIVED every strategy's events on every load channel (counted off the
session's own WebSocket frames), so partial fan-out cannot pass.

Gated like the sibling e2e: ``pytest -m "not e2e"`` skips it; it runs only
under ``ATP_RUN_E2E=1`` with Playwright browsers installed
(``playwright install chromium``).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Iterator

import pytest

# Guard collection: the import must not error when Playwright is absent — the
# collection-time skip in conftest runs *after* module import.
sync_api = pytest.importorskip("playwright.sync_api")

from atp_dashboard import (  # noqa: E402
    LOAD_CHANNELS,
    ReadinessBackedProvider,
    SyntheticStrategyLoad,
    mount_dashboard,
)
from atp_dashboard.publisher import cadence_for  # noqa: E402
from atp_runtime import OperatorInterfaceRuntime  # noqa: E402
from atp_ws import Channel  # noqa: E402

pytestmark = pytest.mark.e2e

#: Hold the load for at least two full METRICS cadences so the ≤5 s refresh is
#: proven *under sustained load*, not just at first paint.
_HOLD_CADENCES = 2


@pytest.fixture()
def loaded_dashboard() -> Iterator[tuple[str, SyntheticStrategyLoad]]:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}))
    load = SyntheticStrategyLoad(runtime)  # NFR-SC1 shape: 1 live + 30 paper
    publisher.start()
    load.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard", load
    finally:
        load.stop()
        publisher.stop()
        runtime.stop()


def test_dashboard_refreshes_within_5s_under_synthetic_nfr_sc1_shaped_load(
    loaded_dashboard: tuple[str, SyntheticStrategyLoad],
) -> None:
    url, load = loaded_dashboard
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()

            # Instrument the measured session's OWN WebSocket: count every
            # synthetic strategy event the browser actually receives, per
            # channel — receipt evidence, not just server-side send counts.
            received: dict[tuple[str, str], int] = defaultdict(int)

            def _count_frame(payload: str | bytes) -> None:
                try:
                    msg = json.loads(payload)
                except (ValueError, TypeError):
                    return
                if not isinstance(msg, dict) or msg.get("type") != "EVENT":
                    return
                channel = msg.get("channel")
                data = msg.get("data")
                sid = data.get("strategy_id") if isinstance(data, dict) else None
                if channel in LOAD_CHANNELS and isinstance(sid, str):
                    if sid.startswith("synthetic-"):
                        received[(channel, sid)] += 1

            page.on(
                "websocket",
                lambda ws: ws.on("framereceived", _count_frame),
            )
            page.goto(url, wait_until="domcontentloaded")

            # Same baseline assertions as the unloaded e2e: panels render, the
            # WebSocket link opens, and the self-measured refresh readout goes
            # numeric within the budget.
            for panel in ("pnl", "metrics", "health", "latency"):
                assert page.locator(f'[data-panel="{panel}"]').count() == 1
            page.wait_for_function(
                "document.getElementById('conn').dataset.state === 'open'", timeout=5_000
            )
            page.wait_for_function(
                "() => { const t = document.getElementById('pulse-value').textContent.trim();"
                " return t && t !== '—' && !Number.isNaN(Number(t.replace(/,/g,''))); }",
                timeout=5_000,
            )

            # Reach steady state before the hold: every required panel fresh.
            for panel in ("pnl", "metrics", "health"):
                page.wait_for_function(
                    f"document.getElementById('fresh-{panel}').dataset.state === 'fresh'",
                    timeout=5_000,
                )

            # Hold under load for ≥ _HOLD_CADENCES full METRICS cadences while
            # the browser keeps processing 31 strategies' worth of PNL/METRICS
            # fan-out, sampling CONTINUOUSLY: the NFR-P2 contract must hold at
            # every sample during the window — a mid-hold breach that recovers
            # before a single end-of-hold check must fail, not pass.
            #
            # The AC bar is the 5 s NFR-P2 budget over the WORST required
            # channel's staleness, which is exactly what `pulse-value` tracks.
            # The per-panel freshness dots grade against each channel's tighter
            # cadence budget (1 s for PNL/HEARTBEAT), so brief `warn` dips there
            # are load jitter within the AC, not a contract breach — but a
            # channel the monitor has never seen (`wait`) fails closed.
            hold_s = _HOLD_CADENCES * cadence_for(Channel.METRICS) + 1
            deadline = time.monotonic() + hold_s
            samples: list[float] = []
            while time.monotonic() < deadline:
                page.wait_for_timeout(250)
                observed = page.evaluate(
                    "Number(document.getElementById('pulse-value').textContent.replace(/,/g, ''))"
                )
                states = page.evaluate(
                    "['pnl','metrics','health'].map("
                    "p => document.getElementById('fresh-' + p).dataset.state)"
                )
                samples.append(observed)
                # NaN fails the <= comparison, so a readout that goes blank
                # mid-hold also fails closed here.
                assert observed <= 5_000, (
                    f"observed refresh {observed}ms breached the NFR-P2 5s budget "
                    f"mid-hold (sample {len(samples)}) under the synthetic "
                    f"NFR-SC1-shaped load"
                )
                assert "wait" not in states, (
                    f"a required channel went unseen mid-hold: "
                    f"pnl/metrics/health = {states} (sample {len(samples)})"
                )
            # The hold actually sampled densely (≈4 samples/s), not vacuously.
            assert len(samples) >= 2 * hold_s
            # The freshness monitor was demonstrably live during the hold (a
            # frozen page would repeat one reading into every sample).
            assert len(set(samples)) > 1, samples

            # Non-vacuity, server side: every strategy published on every
            # channel and the hub delivered to a live subscriber.
            assert len(load.strategy_ids) == 31
            load.assert_load_ran(min_ticks_per_strategy=_HOLD_CADENCES)
            assert load.delivered > 0

            # Non-vacuity, browser side: the measured session RECEIVED every
            # synthetic strategy's events on every load channel at the required
            # tick count — a regression that drops part of the 31-producer
            # fan-out fails here even if the pulse stayed fresh on partial
            # traffic.
            missing = [
                f"{channel}:{sid}={received.get((channel, sid), 0)}"
                for channel in LOAD_CHANNELS
                for sid in load.strategy_ids
                if received.get((channel, sid), 0) < _HOLD_CADENCES
            ]
            assert not missing, (
                f"browser did not receive >= {_HOLD_CADENCES} events for "
                f"{len(missing)} strategy-channel pairs: {sorted(missing)[:8]}"
            )
        finally:
            browser.close()
