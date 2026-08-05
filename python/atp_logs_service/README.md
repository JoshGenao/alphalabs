# atp_logs_service — SRS-LOG-001 log operator surfaces

The operator-facing half of SRS-LOG-001 ("separate persistent system logs from
user strategy logs", SyRS SYS-61 + SYS-38). The storage half — `JsonlLogStore`,
the separated SYSTEM/STRATEGY sinks, `read_records` / `query` — lives in
`atp_logging` and is pinned by
`architecture/runtime_services.json#log_persistence_contract`.

## Why this is a separate package

`tools/log_record_check.py` walks every `python/atp_logging/*.py` and fails on
any import of `atp_api` / `atp_cli` / `atp_ws` / `atp_readiness` / `atp_config` /
`atp_strategy`. `atp_runtime` re-exports the three interface packages, so the
handlers cannot live in the logging SDK without inverting the dependency
direction. This package is the layer that may know both — the same relationship
`atp_logging_boot` has to credential redaction, and `atp_safety` / `atp_readiness`
have to their own handlers.

| Module | Role |
|---|---|
| `handlers.py` | `LogsQueryHandler` — one transport-free handler behind REST `GET /api/v1/logs` and CLI `admin logs` |
| `publisher.py` | `LogEventPublisher` — the event-driven `LOGS` WebSocket channel (its own ticker; the channel declares `refresh_seconds=0`, which the dashboard publisher's cadence guard rejects) |
| `wiring.py` | `wire_logs(runtime, *, system_store_path, strategy_store_path, ...)` |
| `__main__.py` | `python -m atp_logs_service admin logs [...]` — the composed CLI entrypoint |

`atp_dashboard.logs.LogPaneProvider` renders the same records in the dashboard's
log pane and reuses this package's `render_event`, so the REST response, the
published event, and the pane cannot drift into three shapes.

## Wiring (the composer supplies the trail; nothing is implicit)

```python
from atp_logs_service import wire_logs

publisher = wire_logs(
    runtime,
    system_store_path=log_dir / "system.jsonl",
    strategy_store_path=log_dir / "strategy.jsonl",
)
publisher.start()  # claims the LOGS channel HERE, not at wiring time
...
publisher.stop()
```

Store paths are REQUIRED keyword-only arguments with no default: a runtime no
composer wired keeps serving the structured `501` naming `SRS-LOG-001`, and the
`LOGS` channel stays unclaimed so the runtime's own status never reports the
workflow as served. `python -m atp_dashboard` composes both arms from one knob,
`ATP_LOG_DIR`; unset, neither is composed.

For the CLI, `python -m atp_runtime` builds a *bare* runtime, so `admin logs`
there returns the same honest `501` that `kill-switch activate` and `readiness
wait` do. The composed entrypoint is:

```console
$ ATP_LOG_DIR=/var/atp/logs python -m atp_logs_service admin logs --log-class strategy --json
```

`ATP_LOG_DIR` is required with no fallback directory — reporting "no records"
from a guessed path would be worse than refusing.

## The rules this package exists to keep

- **`log_class` selects exactly ONE store.** No merged read on REST/CLI. A
  `source` belonging to the other class is refused, not answered with `[]`.
- **Unreadable is never empty.** A `LogStoreCorruptionError` becomes a
  structured `500` (and `records: null` on the pane). `{"events": []}` for a
  trail that could not be read is indistinguishable from "nothing happened" —
  the failure that makes an audit log worthless. `store_present` further
  separates an absent trail from a present-but-empty one.
- **Undeclared parameters are refused.** Accepting and dropping `?limit=10`
  would report a server-capped page as if the caller's bound had been honoured.
- **Reads are bounded**, newest-first, and report `returned` / `matched` /
  `truncated` rather than implying the page is the whole trail.
- **`--follow` is not declared at all.** `Handler` returns one result and cannot
  stream, so the capability gets no public surface rather than an erroring flag;
  the command summary names the `LOGS` WebSocket channel instead. argparse
  rejects the undeclared flag with a usage error before dispatch — only a
  hand-built `Request` reaches the handler's unknown-parameter guard.
- **The publisher never fabricates and never goes quietly silent.** It publishes
  only records read back from a store; a read failure, or an eviction that costs
  it its place in the trail, is surfaced on `health()` and never cleared.

## Status

SRS-LOG-001 stays `passes:false`. The store and these surfaces are built; what
is missing is the events they would carry — five of the eight SYS-61 system
sources have no producer anywhere in the tree (order routing, ingestion,
container lifecycle, Hot-Swap, resource alerts) and two more are partial — plus
the browser-automation evidence for the dashboard-viewing clause. See
`log_persistence_contract.deferred[]`.
