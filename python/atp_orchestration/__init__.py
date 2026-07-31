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
)

__all__ = [
    "REST_LIFECYCLE_OPERATION",
    "LifecycleActionHandler",
    "RollbackCliRunner",
    "RollbackHandler",
    "mount_rollback",
]
