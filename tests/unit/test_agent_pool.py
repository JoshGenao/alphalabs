"""L1 — Unit tests for the agent scheduler (tools/agent_pool.py).

Covers the pure scheduling/guard logic behind the self-claim model and the four
hardening fixes from the adversarial review:
  - guarded lease reclaim (owner PID-liveness, not just TTL)
  - the honesty guard (needs_serialized) used by `integrate --mode complete`
  - block dependency-id validation
  - ready-frontier / blocked computation, subsystem-avoidance, cycle detection
"""

import os
import socket
import time

import agent_pool
import pytest

pytestmark = pytest.mark.unit


def _feat(fid, category="data", passes=False, priority="P1", description="", steps=None):
    return {
        "id": fid,
        "category": category,
        "priority": priority,
        "passes": passes,
        "needs_clarification": False,
        "description": description,
        "steps": steps or [],
    }


# --- dependency frontier ----------------------------------------------------
def test_compute_blocks_on_unmet_dependency():
    features = [_feat("A", passes=True), _feat("B")]
    deps = {"B": ["A"]}
    ready, blocked, active, held, _ = agent_pool.compute(features, deps, {"leases": {}})
    assert "B" in ready and not blocked  # A passes → B ready

    features = [_feat("A", passes=False), _feat("B")]
    ready, blocked, *_ = agent_pool.compute(features, deps, {"leases": {}})
    assert "B" in blocked and blocked["B"] == ["A"] and "B" not in ready


def test_compute_excludes_active_leases():
    features = [_feat("A"), _feat("B")]
    now = time.time()
    runtime = {"leases": {"A": {"owner": "x:1", "expiry": now + 9999, "port_index": 0}}}
    ready, _blocked, active, _held, _ = agent_pool.compute(features, {}, runtime)
    assert "A" in active and "A" not in ready and "B" in ready


def test_unknown_dependency_is_ignored_not_silently_blocking():
    # compute only counts deps that exist; block() is what must reject typos.
    features = [_feat("B")]
    ready, blocked, *_ = agent_pool.compute(features, {"B": ["NOPE"]}, {"leases": {}})
    assert "B" in ready and not blocked


# --- subsystem-avoidance ordering ------------------------------------------
def test_pick_order_prefers_unheld_subsystem():
    features = [_feat("D1", category="data"), _feat("M1", category="market_data")]
    by_id = {f["id"]: f for f in features}
    held = {"atp-data"}  # a sibling already holds the data crate
    order = agent_pool.pick_order(["D1", "M1"], by_id, held)
    assert order[0] == "M1"  # market_data preferred over the held data crate


# --- cycle detection --------------------------------------------------------
def test_reachable_detects_cycle():
    deps = {"B": ["A"]}  # adding A->B would close A->B->A
    assert agent_pool.reachable(deps, "B", "A") is True
    assert agent_pool.reachable(deps, "A", "B") is False


# --- honesty guard ----------------------------------------------------------
def test_needs_serialized_flags_ib_and_dashboard_but_not_pure_compute():
    ib = _feat(
        "X",
        description="capture live option-chain snapshots from IB",
        steps=["Run an integration test against IB Gateway"],
    )
    need, hits = agent_pool.needs_serialized(ib)
    assert need and hits

    pure = _feat(
        "Y",
        description="consolidate minute bars into 5-minute bars",
        steps=["Resample fixture OHLCV in-process and assert aggregation"],
    )
    need2, hits2 = agent_pool.needs_serialized(pure)
    assert need2 is False and hits2 == []


# --- block id validation ----------------------------------------------------
def test_validate_block_splits_known_and_unknown():
    ids = {"A", "B", "C"}
    known, unknown = agent_pool.validate_block(ids, "A", ["B", "ZZZ"])
    assert known == ["B"] and unknown == ["ZZZ"]


# --- guarded lease reclaim (PID liveness) ----------------------------------
def test_lease_active_honors_live_owner_pid_even_when_expired():
    me = f"{socket.gethostname()}:{os.getpid()}"
    expired_but_alive = {"owner": me, "expiry": time.time() - 10_000}
    assert agent_pool.lease_active(expired_but_alive, time.time()) is True

    dead = {"owner": f"{socket.gethostname()}:2147483646", "expiry": time.time() - 10_000}
    assert agent_pool.lease_active(dead, time.time()) is False

    future = {"owner": "whatever:1", "expiry": time.time() + 10_000}
    assert agent_pool.lease_active(future, time.time()) is True


def test_owner_is_live_rejects_malformed_and_remote():
    assert agent_pool.owner_is_live("") is False
    assert agent_pool.owner_is_live("no-colon") is False
    assert agent_pool.owner_is_live("some-other-host:1") is False  # not this host
    assert agent_pool.owner_is_live(f"{socket.gethostname()}:{os.getpid()}") is True


# --- foreign-host lease stickiness (single-host contract) -------------------
def test_lease_active_foreign_host_is_sticky_unless_reclaiming():
    foreign_expired = {"owner": "other-host:1", "expiry": time.time() - 10_000}
    # default: never auto-reclaim a remote owner on TTL alone (can't probe its pid)
    assert agent_pool.lease_active(foreign_expired, time.time()) is True
    # explicit --reclaim releases it
    assert (
        agent_pool.lease_active(foreign_expired, time.time(), allow_foreign_reclaim=True) is False
    )


# --- integrate staging allowlist (no feature work in the marker commit) -----
def test_path_in_allowlist():
    assert agent_pool.path_in_allowlist("feature_list.json") is True
    assert agent_pool.path_in_allowlist("progress.txt") is True
    assert agent_pool.path_in_allowlist("progress.d/session-SRS-DATA-008.md") is True
    assert agent_pool.path_in_allowlist("tools/feature_deps.json") is True
    # feature / tooling / test work must NOT be stage-able by integrate
    assert agent_pool.path_in_allowlist("tools/agent_pool.py") is False
    assert agent_pool.path_in_allowlist("crates/atp-data/src/store.rs") is False
    assert agent_pool.path_in_allowlist("tests/unit/test_agent_pool.py") is False
