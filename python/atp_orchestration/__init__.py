"""Strategy-orchestration operator handlers (``SRS-ORCH-005`` rollback).

Top-layer consumer package (like ``atp_dashboard``): it composes onto an
:class:`atp_runtime.OperatorInterfaceRuntime` from above via
:func:`mount_rollback`; the runtime never imports it.
"""

from .rollback_handler import (
    REST_LIFECYCLE_OPERATION,
    LifecycleActionHandler,
    RollbackCliRunner,
    RollbackHandler,
    mount_rollback,
    rollback_is_served,
)

__all__ = [
    # The SHARED lifecycle route key (start/stop/restart/rollback). Exported for
    # composers/tests that need to reason about the route itself — but note that
    # registration on it does NOT mean rollback is served: ask
    # `rollback_is_served` for that.
    "REST_LIFECYCLE_OPERATION",
    "LifecycleActionHandler",
    "RollbackCliRunner",
    "RollbackHandler",
    "mount_rollback",
    "rollback_is_served",
]
