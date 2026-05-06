#!/usr/bin/env python3
"""Contract evidence script for feature API-3.

Introspects the declarative ``atp_ws`` package and confirms that the
operator WebSocket API contract exposes every event channel and control
command required by API-3's description, tracing each to ``SRS-UI-001``
through ``SRS-UI-004`` and the supporting clauses listed in
``docs/SRS.md`` §6 and §7.

Mirrors the PASS/FAIL output style of ``tools/rest_api_check.py`` and
``tools/strategy_api_check.py``.

Invoke:
    python3 tools/websocket_api_check.py            # check (exit 0 on PASS)
    python3 tools/websocket_api_check.py --update   # rewrite frozen AsyncAPI snapshot
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
SNAPSHOT_PATH = ROOT / "python" / "atp_ws" / "asyncapi.json"


class WebSocketContractError(AssertionError):
    pass


def fail(message: str) -> None:
    raise WebSocketContractError(message)


def _load() -> object:
    if str(PYTHON_ROOT) not in sys.path:
        sys.path.insert(0, str(PYTHON_ROOT))
    return importlib.import_module("atp_ws")


def _channels_for(module, channel_name: str):
    channel = getattr(module.Channel, channel_name)
    matches = [
        event for event in module.EVENT_CHANNELS if event.name is channel
    ]
    if not matches:
        fail(f"No event channel declared for bucket {channel_name}")
    return matches


def _expect_srs_refs(events, required: Iterable[str]) -> None:
    refs = set()
    for event in events:
        if not event.srs_refs:
            fail(f"Channel {event.name.value} has empty srs_refs")
        refs.update(event.srs_refs)
    missing = sorted(set(required) - refs)
    if missing:
        fail(
            f"Channel is missing required SRS traces: {', '.join(missing)}"
        )


def _expect_payload_fields(events, required: Iterable[str]) -> None:
    fields = set()
    for event in events:
        fields.update(event.payload_fields)
    missing = sorted(set(required) - fields)
    if missing:
        fail(
            "Channel payload missing required fields: "
            f"{', '.join(missing)}"
        )


def _expect_subscription(events) -> None:
    for event in events:
        if not event.requires_subscription:
            fail(
                f"Channel {event.name.value} must require subscription "
                "(SRS-SEC-002 fan-out policy)."
            )


def _expect_refresh_window(events, ceiling: int) -> None:
    for event in events:
        if event.refresh_seconds < 0 or event.refresh_seconds > ceiling:
            fail(
                f"Channel {event.name.value} refresh_seconds="
                f"{event.refresh_seconds} violates [0, {ceiling}] window "
                "(NFR-P2)."
            )


def _summary(label: str, events) -> str:
    items = ", ".join(
        f"{event.name.value} (refresh<={event.refresh_seconds}s)"
        for event in events
    )
    return f"{label}: {items}"


# --------------------------------------------------------------------------- #
# Per-channel checks (one per API-3 bucket)
# --------------------------------------------------------------------------- #


def check_api_3_001_pnl(module) -> str:
    events = _channels_for(module, "PNL")
    _expect_srs_refs(events, ("SRS-UI-001", "SYS-36", "NFR-P2"))
    _expect_payload_fields(
        events, ("strategy_id", "daily_pnl", "cumulative_pnl", "as_of")
    )
    _expect_subscription(events)
    return _summary("PNL (SRS-UI-001, SYS-36, NFR-P2)", events)


def check_api_3_002_metrics(module) -> str:
    events = _channels_for(module, "METRICS")
    _expect_srs_refs(events, ("SRS-UI-001", "SYS-36", "SYS-37"))
    _expect_payload_fields(
        events, ("sharpe", "sortino", "max_drawdown", "benchmark_return")
    )
    return _summary("METRICS (SRS-UI-001, SYS-36, SYS-37)", events)


def check_api_3_003_account_status(module) -> str:
    events = _channels_for(module, "ACCOUNT_STATUS")
    _expect_srs_refs(events, ("SRS-UI-003", "SYS-43b", "SYS-46"))
    _expect_payload_fields(
        events,
        (
            "equity",
            "margin_usage",
            "buying_power",
            "ib_connection_state",
        ),
    )
    return _summary("ACCOUNT_STATUS (SRS-UI-003, SYS-43b, SYS-46)", events)


def check_api_3_004_heartbeat(module) -> str:
    events = _channels_for(module, "HEARTBEAT")
    _expect_srs_refs(events, ("SRS-UI-001", "SYS-39", "SYS-39a"))
    _expect_payload_fields(
        events, ("feed", "staleness_seconds", "is_stale")
    )
    return _summary("HEARTBEAT (SRS-UI-001, SYS-39, SYS-39a)", events)


def check_api_3_005_logs(module) -> str:
    events = _channels_for(module, "LOGS")
    _expect_srs_refs(events, ("SRS-LOG-001", "SYS-38", "SYS-61"))
    _expect_payload_fields(
        events,
        ("timestamp", "severity", "source", "message", "correlation_id"),
    )
    return _summary("LOGS (SRS-LOG-001, SYS-38, SYS-61)", events)


def check_api_3_006_alerts(module) -> str:
    events = _channels_for(module, "ALERTS")
    _expect_srs_refs(events, ("SRS-NOTIF-001", "SYS-46", "SYS-58"))
    _expect_payload_fields(
        events,
        ("alert_id", "severity", "delivery_status", "acknowledged"),
    )
    return _summary("ALERTS (SRS-NOTIF-001, SYS-46, SYS-58)", events)


def check_api_3_007_reservoir_ranking(module) -> str:
    events = _channels_for(module, "RESERVOIR_RANKING")
    _expect_srs_refs(events, ("SRS-RESV-002", "SYS-48", "SRS-UI-003"))
    _expect_payload_fields(
        events, ("rankings", "sharpe", "sortino", "momentum_score")
    )
    return _summary("RESERVOIR_RANKING (SRS-RESV-002, SYS-48)", events)


def check_api_3_008_strategy_state(module) -> str:
    events = _channels_for(module, "STRATEGY_STATE")
    _expect_srs_refs(events, ("SRS-UI-002", "SYS-41", "SYS-79"))
    _expect_payload_fields(
        events,
        (
            "strategy_id",
            "mode",
            "container_status",
            "deployment_version_hash",
            "position_count",
        ),
    )
    return _summary("STRATEGY_STATE (SRS-UI-002, SYS-41, SYS-79)", events)


# --------------------------------------------------------------------------- #
# Cross-cutting checks
# --------------------------------------------------------------------------- #


def check_api_3_009_subscribe_protocol(module) -> str:
    """SUBSCRIBE / UNSUBSCRIBE commands must accept a channels payload."""

    sub = [
        c for c in module.CLIENT_COMMANDS if c.type is module.MessageType.SUBSCRIBE
    ]
    unsub = [
        c
        for c in module.CLIENT_COMMANDS
        if c.type is module.MessageType.UNSUBSCRIBE
    ]
    if not sub:
        fail("CLIENT_COMMANDS missing SUBSCRIBE entry")
    if not unsub:
        fail("CLIENT_COMMANDS missing UNSUBSCRIBE entry")
    for command in (*sub, *unsub):
        if "channels" not in command.request_fields:
            fail(
                f"{command.type.value} command must accept 'channels' "
                "field for fan-out targeting."
            )
        if command.response_message is not module.MessageType.ACK:
            fail(
                f"{command.type.value} command must reply with ACK; "
                f"got {command.response_message.value}."
            )
    return "Subscribe/unsubscribe protocol routes channels list and returns ACK"


def check_api_3_010_heartbeat_protocol(module) -> str:
    """HEARTBEAT_PING command must pair with HEARTBEAT_PONG and trace SYS-39."""

    pings = [
        c
        for c in module.CLIENT_COMMANDS
        if c.type is module.MessageType.HEARTBEAT_PING
    ]
    if not pings:
        fail("CLIENT_COMMANDS missing HEARTBEAT_PING entry (SYS-39)")
    for command in pings:
        if command.response_message is not module.MessageType.HEARTBEAT_PONG:
            fail(
                "HEARTBEAT_PING command must reply with HEARTBEAT_PONG; "
                f"got {command.response_message.value}."
            )
        if "SYS-39" not in command.srs_refs:
            fail("HEARTBEAT_PING command must trace SYS-39.")
    return "HEARTBEAT_PING/PONG protocol present and traces SYS-39"


def check_api_3_011_refresh_budget(module) -> str:
    """Every event channel must respect the NFR-P2 refresh ceiling."""

    ceiling = module.MAX_REFRESH_SECONDS
    _expect_refresh_window(module.EVENT_CHANNELS, ceiling)
    if ceiling != 5:
        fail(
            f"MAX_REFRESH_SECONDS must be 5 (NFR-P2 dashboard refresh "
            f"ceiling); got {ceiling}."
        )
    return f"All channels refresh within [0, {ceiling}]s (NFR-P2)"


def check_api_3_012_asyncapi_snapshot(module) -> str:
    """Snapshot of the AsyncAPI dict must be byte-equal to the committed file."""

    if not SNAPSHOT_PATH.exists():
        fail(
            "AsyncAPI snapshot is missing; "
            "run: python3 tools/websocket_api_check.py --update"
        )
    actual = SNAPSHOT_PATH.read_text(encoding="utf-8")
    expected = module.render_snapshot()
    if actual != expected:
        fail(
            "AsyncAPI snapshot is stale; "
            "regenerate via: python3 tools/websocket_api_check.py --update"
        )
    return f"AsyncAPI snapshot in sync ({SNAPSHOT_PATH.relative_to(ROOT)})"


def check_api_3_013_loopback_policy(module) -> str:
    """Bind host, auth model, and WS path encode SRS-SEC-002."""

    if module.BIND_HOST != "127.0.0.1":
        fail(
            f"BIND_HOST must be 127.0.0.1 (SRS-SEC-002); "
            f"got {module.BIND_HOST!r}"
        )
    if module.AUTH_MODEL != "local-single-user":
        fail(
            "AUTH_MODEL must be 'local-single-user' (SRS-SEC-002); "
            f"got {module.AUTH_MODEL!r}"
        )
    if module.WS_PATH != "/ws/v1":
        fail(f"WS_PATH must be '/ws/v1'; got {module.WS_PATH!r}")
    document = module.build_asyncapi()
    for key, expected in (
        ("x-bind-host", module.BIND_HOST),
        ("x-auth-model", module.AUTH_MODEL),
        ("x-ws-path", module.WS_PATH),
    ):
        if document.get(key) != expected:
            fail(
                f"AsyncAPI document missing/incorrect {key}: "
                f"expected {expected!r}, got {document.get(key)!r}"
            )
    return "Loopback bind 127.0.0.1 /ws/v1 + local-single-user (SRS-SEC-002)"


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


def _channel_coverage(module) -> None:
    declared = {event.name for event in module.EVENT_CHANNELS}
    expected = set(module.Channel)
    missing = sorted(c.value for c in (expected - declared))
    if missing:
        fail(f"EVENT_CHANNELS missing buckets: {', '.join(missing)}")


def run_checks() -> list[str]:
    module = _load()
    _channel_coverage(module)

    evidence: list[str] = []
    for check in (
        check_api_3_001_pnl,
        check_api_3_002_metrics,
        check_api_3_003_account_status,
        check_api_3_004_heartbeat,
        check_api_3_005_logs,
        check_api_3_006_alerts,
        check_api_3_007_reservoir_ranking,
        check_api_3_008_strategy_state,
        check_api_3_009_subscribe_protocol,
        check_api_3_010_heartbeat_protocol,
        check_api_3_011_refresh_budget,
        check_api_3_012_asyncapi_snapshot,
        check_api_3_013_loopback_policy,
    ):
        evidence.append(check(module))
    return evidence


def update_snapshot() -> str:
    module = _load()
    SNAPSHOT_PATH.write_text(module.render_snapshot(), encoding="utf-8")
    return f"Wrote {SNAPSHOT_PATH.relative_to(ROOT)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="API-3 contract evidence")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Regenerate the frozen AsyncAPI snapshot from atp_ws.EVENT_CHANNELS.",
    )
    args = parser.parse_args(argv)

    if args.update:
        try:
            message = update_snapshot()
        except Exception as error:  # noqa: BLE001 - surfacing all import/IO errors
            print(f"API-3 UPDATE FAIL: {error}", file=sys.stderr)
            return 1
        print(message)
        return 0

    try:
        evidence = run_checks()
    except WebSocketContractError as error:
        print(f"API-3 FAIL: {error}", file=sys.stderr)
        return 1

    print("API-3 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
