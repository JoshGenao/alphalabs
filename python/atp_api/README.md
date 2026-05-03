# `atp_api` — REST API contract for the ATP operator surface

This package is the **contract** for the REST API described by ``API-2`` in
``feature_list.json`` and ``SRS-API-001`` in ``docs/SRS.md`` §7. It does not
contain HTTP handlers — those land with the downstream features that own
each capability (``EXE-1``, ``ORCH-1``, ``RESV-1``, ``BT-1``, ``DATA-1``,
``LOG-1``, ``NOTIF-1``).

The contract surface is introspectable at runtime via ``atp_api.ROUTES`` and
is rendered to a frozen OpenAPI 3.1 snapshot at
[`python/atp_api/openapi.json`](./openapi.json). The snapshot is verified by
``tools/rest_api_check.py``.

## Auth and bind policy (SRS-SEC-002)

* `BIND_HOST = "127.0.0.1"` — loopback only by default; RFC 1918 binds are
  permitted but not exposed to the public internet.
* `AUTH_MODEL = "local-single-user"` — the platform is single-operator
  (``StRS C-6``); no bearer tokens, sessions, or RBAC are defined.

## Capabilities

| Capability | Routes | SRS trace |
|---|---|---|
| `STRATEGY_LIFECYCLE` | `GET /api/v1/strategies`, `POST /api/v1/strategies/{id}/lifecycle` | SRS-ORCH-004, SRS-ORCH-005, SYS-2c, SYS-79, SYS-80 |
| `LIVE_DESIGNATION` | `POST /api/v1/strategies/{id}/promote-live` | SYS-2c, SYS-2d |
| `KILL_SWITCH` | `POST /api/v1/kill-switch` | SRS-SAFE-001, SYS-44a, SYS-44b, NFR-P3 |
| `HOT_SWAP` | `POST /api/v1/hot-swap`, `GET /api/v1/hot-swap/status` | SRS-RESV-003..006, SYS-49a..e |
| `BACKTEST_LAUNCH` | `POST /api/v1/backtests` | SRS-BT-001, SYS-14, SYS-43a |
| `BACKTEST_QUERY` | `GET /api/v1/backtests/{id}` | SRS-BT-009, SYS-21, SYS-42 |
| `RESERVOIR_RANKING` | `GET /api/v1/reservoir/ranking` | SRS-RESV-002, SYS-48 |
| `WATCHLIST_CONFIG` | `GET /api/v1/watchlist`, `PUT /api/v1/watchlist` | SRS-DATA-002, SYS-22b |
| `SYSTEM_STATUS` | `GET /api/v1/system/status` | SYS-76, SYS-39, SYS-58 |
| `LOGS` | `GET /api/v1/logs` | SRS-LOG-001, SYS-38, SYS-61 |
| `ALERTS` | `GET /api/v1/alerts` | SRS-NOTIF-001, SYS-46, SYS-58 |

## Confirmation routes

Routes that perform irreversible actions carry `requires_confirmation=True`
and accept a `confirm` query parameter. The two-step UX (modal + token) is
defined by ``UI-4`` and lands with the dashboard feature.

* `POST /api/v1/strategies/{id}/promote-live` — SYS-2d
* `POST /api/v1/kill-switch` — SRS-SAFE-001, SYS-44a, NFR-P3

## Sample requests

When the handler runtime exists (downstream features), interaction will look
like:

```
# System status
curl -sf http://127.0.0.1:8080/api/v1/system/status

# Kill switch (irreversible — confirmation required)
curl -sf -X POST 'http://127.0.0.1:8080/api/v1/kill-switch?confirm=YES'
```

## Regenerating the OpenAPI snapshot

The snapshot is byte-frozen: every change to ``ROUTES`` must be reflected in
``openapi.json``.

```
python3 tools/rest_api_check.py --update
git diff -- python/atp_api/openapi.json
```

## Verification

```
python3 tools/rest_api_check.py        # → "API-2 PASS"
python3 -m unittest tests.test_rest_api
```
