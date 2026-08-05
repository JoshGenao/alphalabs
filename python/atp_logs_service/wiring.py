"""Register the real SRS-LOG-001 log surfaces on the operator runtime.

Mirrors ``atp_safety.wiring`` / ``atp_readiness.wiring``: every collaborator is
a REQUIRED keyword-only argument with no default and no fixture fallback — the
composer owns the honesty of each choice. A bare
:class:`~atp_runtime.runtime.OperatorInterfaceRuntime` (one no composer wired)
keeps serving the structured deferred ``501`` for ``GET /api/v1/logs`` and
``admin logs``, and the ``LOGS`` channel stays unclaimed so the runtime's own
status never reports the LOGS workflow as served.

The store *paths* are what gets wired, not open store handles: the query
surface and the publisher are readers of a trail some other process
(``atp_safety``'s kill-switch handler, the SRS-MD-003 heartbeat monitor, a
future core-runtime forwarder) owns and writes. Handing them a writer's
:class:`~atp_logging.persistence.JsonlLogStore` would put a second writer's lock
in front of a read-only surface for no benefit.

SRS trace
---------
``SRS-LOG-001`` (the LOGS REST/CLI/WS operations the runtime owner map
attributes to this feature), ``SRS-API-001`` (registry/dispatch substrate).
"""

from __future__ import annotations

import os
from functools import partial

from atp_runtime.registry import OperationKey, Surface
from atp_runtime.runtime import OperatorInterfaceRuntime

from .handlers import (
    DEFAULT_MAX_EVENTS,
    LOGS_CLI_OPERATION,
    LOGS_REST_OPERATION,
    LogsQueryHandler,
)
from .publisher import DEFAULT_POLL_INTERVAL_S, LOGS_CHANNEL, LogEventPublisher

__all__ = ["LOGS_OPERATIONS", "wire_logs"]

#: The SDK-pinned operations this package makes real.
LOGS_OPERATIONS: tuple[OperationKey, ...] = (
    OperationKey(Surface.REST, LOGS_REST_OPERATION),
    OperationKey(Surface.CLI, LOGS_CLI_OPERATION),
)


def wire_logs(
    runtime: OperatorInterfaceRuntime,
    *,
    system_store_path: str | os.PathLike[str],
    strategy_store_path: str | os.PathLike[str],
    max_events: int = DEFAULT_MAX_EVENTS,
    max_files: int = 5,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> LogEventPublisher:
    """Bind the log query handler + ``LOGS`` publisher to ``runtime``.

    Args:
        runtime: The operator-interface runtime to register on.
        system_store_path: Active segment of the SYSTEM store — REQUIRED, no
            default. The composer chooses which trail this operator interface
            serves; there is no implicit directory to fall back on.
        strategy_store_path: Active segment of the STRATEGY store — REQUIRED,
            and validated to be a different physical file (the SRS-LOG-001
            separation invariant, re-asserted at the read surface).
        max_events: Cap on events in one query response (newest-first).
        max_files: Rotation depth of the stores; must match the writer's so no
            rotated segment is invisible.
        poll_interval_s: How often the publisher looks for newly-appended
            records.

    Returns:
        The un-started :class:`~atp_logs_service.publisher.LogEventPublisher`.
        The caller starts and stops it, exactly as ``mount_dashboard`` returns
        an un-started ``DashboardPublisher`` — a publisher that started itself
        inside a wiring call could outlive a failed composition.
    """

    handler = LogsQueryHandler(
        system_store_path=system_store_path,
        strategy_store_path=strategy_store_path,
        max_events=max_events,
        max_files=max_files,
    )
    registry = runtime.registry
    registry.register(OperationKey(Surface.REST, LOGS_REST_OPERATION), handler)
    registry.register(OperationKey(Surface.CLI, LOGS_CLI_OPERATION), handler)

    return LogEventPublisher(
        publish=runtime.publish,
        claim_channel=partial(runtime.register_publisher, LOGS_CHANNEL),
        release_channel=partial(runtime.unregister_publisher, LOGS_CHANNEL),
        system_store_path=system_store_path,
        strategy_store_path=strategy_store_path,
        poll_interval_s=poll_interval_s,
        max_files=max_files,
    )
