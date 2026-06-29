"""Runtime-owned handlers — the operations the interface serves itself.

Every *domain* operation (kill switch, lifecycle, ranking, backtests, logs,
alerts, ...) is deferred to its owning feature via
:class:`atp_runtime.registry.DeferredHandler`. The operator-interface runtime
owns only the operations that describe *itself* and need no downstream feature:

* :class:`SystemStatusHandler` — the runtime's own liveness + a per-workflow
  implemented/deferred map. ``ready`` is ``False`` while any required workflow
  is still deferred, so the report never overstates trade-readiness (the
  *domain* readiness checks — IB / SSD / NAS / ingestion freshness — belong to
  ``SRS-MD-006``).
* :class:`VersionHandler` — runtime + contract version.
* :class:`ConfigHandler` — the configuration *schema* with secret values always
  redacted (proves the ``SRS-SEC-001`` no-plaintext-secret policy at the
  interface layer).

SRS trace
---------
``SRS-API-001``; ``SYS-76`` (status report shape), ``SRS-SEC-001`` (config
redaction), ``SRS-SEC-002`` (bind host / auth model surfaced honestly).
"""

from __future__ import annotations

from collections.abc import Callable

from .registry import HandlerResult, Request

#: Version of the operator-interface runtime itself (distinct from the per-
#: surface contract versions in atp_api / atp_cli / atp_ws).
RUNTIME_VERSION = "0.1.0"


class SystemStatusHandler:
    """Serve the runtime's own status report (``GET /api/v1/system/status``).

    Built with a ``status_fn`` so the runtime can inject a live snapshot
    (surface counts, per-workflow handler registration) without this handler
    importing the assembly module.
    """

    def __init__(self, status_fn: Callable[[], dict]) -> None:
        self._status_fn = status_fn

    def handle(self, request: Request) -> HandlerResult:
        return HandlerResult(200, self._status_fn())


class VersionHandler:
    """Serve runtime + contract version metadata."""

    def __init__(self, contract_revision: str) -> None:
        self._contract_revision = contract_revision

    def handle(self, request: Request) -> HandlerResult:
        return HandlerResult(
            200,
            {
                "component": "atp-operator-interface-runtime",
                "runtime_version": RUNTIME_VERSION,
                "contract_revision": self._contract_revision,
                "srs_ref": "SRS-API-001",
            },
        )


class ConfigHandler:
    """Serve the configuration schema with secret values redacted.

    The handler never reads or emits a secret *value*: it returns the declared
    key catalogue (name, category, type, ``secret`` flag), with secret keys
    rendered as a constant redaction marker. This demonstrates the
    ``SRS-SEC-001`` no-plaintext-secret-logging policy at the operator surface.
    """

    REDACTION_MARKER = "***REDACTED***"

    def __init__(self, key_catalogue: list[dict]) -> None:
        self._key_catalogue = key_catalogue

    def handle(self, request: Request) -> HandlerResult:
        return HandlerResult(200, {"keys": self._key_catalogue})
