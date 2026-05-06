# `atp_ws` — WebSocket API contract for ATP dashboard updates

This package is the **contract** for the WebSocket API described by
``API-3`` in ``feature_list.json`` and traced to ``SRS-UI-001`` through
``SRS-UI-004`` in ``docs/SRS.md`` §7. It does not contain a WebSocket
runtime — concrete publishers land with the downstream features that
own each event source (``EXE-1``, ``ORCH-1``, ``MD-1``, ``RESV-1``,
``LOG-1``, ``NOTIF-1``).

The contract surface is introspectable at runtime via
``atp_ws.EVENT_CHANNELS`` and ``atp_ws.CLIENT_COMMANDS``, and is
rendered to a frozen AsyncAPI 2.6 snapshot at
[`python/atp_ws/asyncapi.json`](./asyncapi.json). The snapshot is
verified by ``tools/websocket_api_check.py``.

## Auth and bind policy (SRS-SEC-002)

* `BIND_HOST = "127.0.0.1"` — loopback only by default; RFC 1918 binds
  are permitted but not exposed to the public internet.
* `AUTH_MODEL = "local-single-user"` — the platform is single-operator
  (``StRS C-6``); no bearer tokens, sessions, or RBAC are defined.
* `WS_PATH = "/ws/v1"` — versioned multiplexed endpoint; all channels
  ride a single socket and are addressed by the ``channel`` field.

## Event channels (server → client)

| Channel | Refresh | SRS trace |
|---|---|---|
| `PNL` | ≤ 1 s | SRS-UI-001, SYS-36, NFR-P2 |
| `METRICS` | ≤ 5 s | SRS-UI-001, SYS-36, SYS-37 |
| `ACCOUNT_STATUS` | ≤ 5 s | SRS-UI-003, SYS-43b, SYS-46 |
| `HEARTBEAT` | ≤ 1 s | SRS-UI-001, SYS-39, SYS-39a |
| `LOGS` | event-driven | SRS-LOG-001, SYS-38, SYS-61 |
| `ALERTS` | event-driven | SRS-NOTIF-001, SYS-46, SYS-58 |
| `RESERVOIR_RANKING` | ≤ 5 s | SRS-RESV-002, SYS-48, SRS-UI-003 |
| `STRATEGY_STATE` | ≤ 5 s | SRS-UI-002, SYS-41, SYS-79 |

`refresh_seconds=0` means *event-driven* — payloads are emitted when
the underlying state changes, with no fixed cadence. All other channels
are bound by ``NFR-P2 ≤ 5 s``.

## Message types

| Type | Direction | Purpose |
|---|---|---|
| `EVENT` | server → client | Channel payload publish. |
| `SUBSCRIBE` | client → server | Add channels to the socket. |
| `UNSUBSCRIBE` | client → server | Remove channels from the socket. |
| `ACK` | server → client | Reply to SUBSCRIBE / UNSUBSCRIBE. |
| `ERROR` | server → client | Structured failure envelope. |
| `HEARTBEAT_PING` | client → server | Liveness probe. |
| `HEARTBEAT_PONG` | server → client | Liveness reply (SYS-39). |

## Sample frames

```
# Client subscribes to two channels
{ "type": "SUBSCRIBE", "channels": ["PNL", "HEARTBEAT"] }

# Server acknowledges
{ "type": "ACK", "subscribed": ["PNL", "HEARTBEAT"] }

# Server publishes a P&L event
{
  "type": "EVENT",
  "channel": "PNL",
  "data": {
    "strategy_id": "alpha-momentum-1",
    "daily_pnl": 1234.56,
    "cumulative_pnl": 9876.54,
    "unrealized_pnl": 42.0,
    "as_of": "2026-05-05T16:00:01Z"
  }
}
```

## Regenerating the AsyncAPI snapshot

The snapshot is byte-frozen: every change to ``EVENT_CHANNELS`` or
``CLIENT_COMMANDS`` must be reflected in ``asyncapi.json``.

```
python3 tools/websocket_api_check.py --update
git diff -- python/atp_ws/asyncapi.json
```

## Verification

```
python3 tools/websocket_api_check.py        # → "API-3 PASS"
python3 -m unittest tests.test_websocket_api
```
