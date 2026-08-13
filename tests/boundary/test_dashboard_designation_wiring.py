"""L4 boundary — ``SRS-RESV-005``'s live-designation leg on the UI-5 pane.

The pane's ``current_live_strategy_id`` cell was ``deferred:SRS-RESV-005`` in every
prior session. It now resolves from the DURABLE designation snapshot
``resv005_hot_swap_promote_cli`` maintains — the same record the promotion gate
writes — so the pane reports the authority itself rather than a second copy that
could drift from it.

Composed as a third leg beside SRS-RESV-004's demotion leg, and these tests pin the
properties that make that safe:

* the cell resolves ONLY from a real readable record;
* an unreadable record is an ``ok:false`` error, never "no strategy is live";
* a genuinely-empty record defers (an under-claim) rather than asserting anything;
* the two legs MERGE — one feature's fact does not evict the other's;
* an unconfigured deployment keeps the deferred placeholder, so mounting the pane
  never starts claiming a fact this runtime has no producer for.

SRS trace: ``SRS-RESV-005``, ``SyRS SYS-49d``, ``UI-5`` (pane), ``SRS-RESV-004``
(the sibling leg it merges with).
"""

from __future__ import annotations

import subprocess

import pytest
from atp_dashboard import HotSwapStatusProvider
from atp_hotswap import CliHotSwapPromotionSource, CompositeHotSwapStatusSource

pytestmark = pytest.mark.boundary

STATE_MAGIC = "RESV005-LIVE-DESIGNATION-STATE v1"
PROMOTION_OWNER = "deferred:SRS-RESV-005"


class _FakeCli:
    """Replays scripted ``key:value`` stdout, like the real binary."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self._result = (returncode, stdout, stderr)

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        code, out, err = self._result
        return subprocess.CompletedProcess(argv, code, out, err)


def _snapshot(source) -> dict:
    return HotSwapStatusProvider(source=source).hot_swap_snapshot()


def _promotion(cli, tmp_path):
    return CliHotSwapPromotionSource(tmp_path / "live.state", binary=tmp_path / "bin", runner=cli)


# --------------------------------------------------------------------------- #
# The cell resolves from the real record
# --------------------------------------------------------------------------- #


def test_a_designated_strategy_resolves_the_cell(tmp_path):
    cli = _FakeCli(stdout="designated:momentum-v3\n")

    snap = _snapshot(CompositeHotSwapStatusSource(promotion=_promotion(cli, tmp_path)))

    assert snap["ok"] is True
    assert snap["current_live_strategy_id"]["value"] == "momentum-v3"
    # Provenance: read from state, NOT the deferred placeholder it used to carry.
    assert snap["current_live_strategy_id"]["data_source"] != PROMOTION_OWNER


def test_an_unconfigured_deployment_keeps_the_deferred_placeholder(tmp_path):
    # No promotion leg composed at all — the pre-SRS-RESV-005 posture, preserved.
    snap = _snapshot(CompositeHotSwapStatusSource())

    assert snap["ok"] is True
    assert snap["current_live_strategy_id"]["value"] is None
    assert snap["current_live_strategy_id"]["data_source"] == PROMOTION_OWNER


# --------------------------------------------------------------------------- #
# Unreadable is never "no strategy is live"
# --------------------------------------------------------------------------- #


def test_an_unreadable_record_is_an_error_not_an_empty_slot(tmp_path):
    cli = _FakeCli(returncode=1, stderr=f"state file is not a {STATE_MAGIC} snapshot")

    snap = _snapshot(CompositeHotSwapStatusSource(promotion=_promotion(cli, tmp_path)))

    # ok:false is the load-bearing part — the pane shows the failure and the promote
    # control goes inert. Reading it as "nothing is live" would arm a swap over a
    # strategy that may well be running.
    assert snap["ok"] is False
    assert any("live designation unreadable" in e for e in snap["errors"])
    assert snap["current_live_strategy_id"]["value"] is None


def test_a_missing_designated_line_is_refused(tmp_path):
    cli = _FakeCli(stdout="something-else:true\n")

    snap = _snapshot(CompositeHotSwapStatusSource(promotion=_promotion(cli, tmp_path)))

    assert snap["ok"] is False
    assert any("designated" in e for e in snap["errors"])


def test_a_genuinely_empty_record_defers_rather_than_asserting(tmp_path):
    cli = _FakeCli(stdout="designated:none\n")

    snap = _snapshot(CompositeHotSwapStatusSource(promotion=_promotion(cli, tmp_path)))

    # Not an error — the record was read. But the cell vocabulary cannot say
    # "genuinely nothing is live", so it under-claims. Safe direction: the control
    # stays inert, and the gate would refuse the swap anyway.
    assert snap["ok"] is True
    assert snap["current_live_strategy_id"]["value"] is None


# --------------------------------------------------------------------------- #
# The two legs merge
# --------------------------------------------------------------------------- #


class _DemotionLeg:
    """Stands in for SRS-RESV-004's leg, which owns the OTHER half of live_state."""

    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    def live_state(self):
        if self._error is not None:
            raise self._error
        return self._payload


def test_both_legs_contribute_to_one_live_state(tmp_path):
    cli = _FakeCli(stdout="designated:momentum-v3\n")
    demotion = _DemotionLeg({"demotion_pending": False, "demotion_detail": "clear"})

    snap = _snapshot(
        CompositeHotSwapStatusSource(promotion=_promotion(cli, tmp_path), demotion=demotion)
    )

    assert snap["ok"] is True
    # Neither feature's fact evicts the other's — the merge is a union.
    assert snap["current_live_strategy_id"]["value"] == "momentum-v3"
    assert snap["demotion_pending"]["value"] is False


def test_a_failing_sibling_leg_does_not_fabricate_a_live_strategy(tmp_path):
    from atp_hotswap import HotSwapStatusUnavailable

    cli = _FakeCli(stdout="designated:momentum-v3\n")
    demotion = _DemotionLeg(error=HotSwapStatusUnavailable("lockout unreadable"))

    snap = _snapshot(
        CompositeHotSwapStatusSource(promotion=_promotion(cli, tmp_path), demotion=demotion)
    )

    # Both halves feed ONE protocol method, so an unreadable lockout defers the live
    # strategy too. That is the fail-closed direction and it is deliberate: the pane
    # reports the failure and the control goes inert rather than showing half a truth.
    assert snap["ok"] is False
    assert snap["current_live_strategy_id"]["value"] is None
