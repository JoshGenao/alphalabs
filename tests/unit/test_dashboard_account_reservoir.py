"""L1 unit — SRS-UI-003 account-status + Reservoir-ranking providers.

Both panels are all-deferred today (their producers — the SRS-EXE-006 live IB
adapter and the SRS-RESV-002 SYS-48 ranking engine — are unbuilt), so these
tests pin the honesty contract: every event / snapshot carries exactly the WS
channel's declared ``payload_fields``, every deferred cell has ``value is None``
tagged with the right owner, ``rankings`` is a deferred cell (never an empty
list masquerading as "ranked, zero strategies"), and the real SYS-48 window
constants (1/7/15/30/60/90, default 30) are surfaced. It also guards that the
new providers do NOT bleed into ``ReadinessBackedProvider`` (which must keep
rejecting the two channels).

SRS trace: SRS-UI-003 (account + Reservoir panels), SYS-43b (account view),
SYS-48 (ranking window), SRS-EXE-006 / SRS-RESV-002 (deferred producers).
"""

from __future__ import annotations

import re

import pytest
from atp_dashboard import (
    ALLOWED_EVAL_WINDOWS,
    DEFAULT_EVAL_WINDOW,
    AccountStatusProvider,
    ReadinessBackedProvider,
    ReservoirRankingProvider,
    cadence_for,
)
from atp_dashboard.provider import DashboardMetricsProvider
from atp_ws import EVENT_CHANNELS

pytestmark = pytest.mark.unit

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _payload_fields(channel: str) -> set[str]:
    return set(next(c.payload_fields for c in EVENT_CHANNELS if c.name == channel))


def _is_deferred_cell(cell: object, owner: str) -> bool:
    return (
        isinstance(cell, dict)
        and cell.get("value") is None
        and cell.get("data_source") == f"deferred:{owner}"
    )


# --------------------------------------------------------------------------- #
# ACCOUNT_STATUS (SRS-UI-003 / SYS-43b)
# --------------------------------------------------------------------------- #


def test_account_event_keys_match_the_channel_contract() -> None:
    event = AccountStatusProvider().account_status_events()
    assert len(event) == 1  # one account, not a per-strategy collection
    assert set(event[0]) >= _payload_fields("ACCOUNT_STATUS")
    assert _ISO_Z.match(str(event[0]["as_of"]))


def test_account_fields_are_all_honest_deferred_cells() -> None:
    event = AccountStatusProvider().account_status_events()[0]
    for field in (
        "equity",
        "daily_pnl",
        "cumulative_pnl",
        "margin_usage",
        "buying_power",
        "ib_connection_state",
    ):
        assert _is_deferred_cell(event[field], "SRS-EXE-006"), (
            f"{field} not an honest deferred cell"
        )


def test_account_snapshot_is_ok_and_deferred() -> None:
    snap = AccountStatusProvider().account_snapshot()
    assert snap["ok"] is True
    assert snap["srs_ref"] == "SRS-UI-003"
    assert _ISO_Z.match(str(snap["generated_at"]))
    assert _is_deferred_cell(snap["equity"], "SRS-EXE-006")


# --------------------------------------------------------------------------- #
# RESERVOIR_RANKING (SRS-UI-003 / SYS-48)
# --------------------------------------------------------------------------- #


def test_reservoir_event_keys_match_the_channel_contract() -> None:
    event = ReservoirRankingProvider().reservoir_ranking_events()
    assert len(event) == 1
    assert set(event[0]) >= _payload_fields("RESERVOIR_RANKING")
    assert _ISO_Z.match(str(event[0]["as_of"]))


def test_reservoir_ranking_fields_are_deferred_and_rankings_is_not_empty_list() -> None:
    event = ReservoirRankingProvider().reservoir_ranking_events()[0]
    for field in ("eval_window_days", "rankings", "sharpe", "sortino", "momentum_score"):
        assert _is_deferred_cell(event[field], "SRS-RESV-002"), (
            f"{field} not an honest deferred cell"
        )
    # rankings is a deferred cell (value None), NOT an empty list — an empty list
    # would masquerade as "ranked, zero strategies".
    assert event["rankings"]["value"] is None
    assert event["rankings"]["value"] != []


def test_reservoir_snapshot_exposes_real_sys48_window_config() -> None:
    snap = ReservoirRankingProvider().reservoir_snapshot()
    assert snap["ok"] is True
    assert snap["srs_ref"] == "SRS-UI-003"
    # SYS-48 selector configuration is REAL constant data (a UI input).
    assert snap["allowed_windows"] == [1, 7, 15, 30, 60, 90] == list(ALLOWED_EVAL_WINDOWS)
    assert snap["default_window"] == 30 == DEFAULT_EVAL_WINDOW
    assert snap["default_window"] in snap["allowed_windows"]
    # The ranking RESULT stays deferred.
    assert _is_deferred_cell(snap["rankings"], "SRS-RESV-002")


# --------------------------------------------------------------------------- #
# Cadence + isolation from the SRS-UI-001 provider
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("channel", ["ACCOUNT_STATUS", "RESERVOIR_RANKING"])
def test_channels_declare_a_legal_five_second_cadence(channel: str) -> None:
    assert cadence_for(channel) == 5  # ≤ MAX_REFRESH_SECONDS (NFR-P2)


@pytest.mark.parametrize("provider_cls", [AccountStatusProvider, ReservoirRankingProvider])
def test_new_providers_are_not_readiness_backed(provider_cls: type) -> None:
    provider = provider_cls()
    assert not isinstance(provider, ReadinessBackedProvider)
    assert not isinstance(provider, DashboardMetricsProvider)


@pytest.mark.parametrize("channel", ["ACCOUNT_STATUS", "RESERVOIR_RANKING"])
def test_readiness_provider_still_rejects_the_new_channels(channel: str) -> None:
    # SRS-UI-001's provider owns only PNL/METRICS/HEARTBEAT; the new channels are
    # separate opt-in providers, so this must keep raising (no scope creep).
    with pytest.raises(ValueError):
        ReadinessBackedProvider({}).channel_payload(channel)
