"""L3 contract test for the SRS-LOG-001 persistent-sink runtime half.

Two layers of evidence:

* :func:`test_check_script_passes` runs ``tools/log_persistence_check.py`` as
  a subprocess so the positive-evidence path stays under CI coverage (the
  17 collectors that exercise the persistent sinks behaviourally).
* the parity assertions cross-check the ``log_persistence_contract`` block
  against the imported ``atp_logging.persistence`` module without a
  subprocess (required exports, module path, deferred downstream halves).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_logging import persistence as persistence_module  # noqa: E402

pytestmark = [pytest.mark.contract]

_CONTRACT = json.loads((ROOT / "architecture" / "runtime_services.json").read_text())[
    "log_persistence_contract"
]


def test_check_script_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "log_persistence_check.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "SRS-LOG-001 PERSISTENCE PASS" in result.stdout


def test_required_exports_match() -> None:
    assert sorted(persistence_module.__all__) == sorted(_CONTRACT["required_exports"])


def test_module_path_exists() -> None:
    assert (ROOT / _CONTRACT["module_path"]).exists()


def test_deferred_names_what_actually_blocks_the_flip() -> None:
    """The deferred list names the REAL blockers, not halves already built.

    The dashboard pane (was SRS-UI-001) and the REST/CLI/WS handlers (was
    SRS-API-001) shipped with the ``operator_surface`` block; what keeps
    SRS-LOG-001 at ``passes:false`` is the missing producers, the core-runtime
    forwarding path, and the browser-automation evidence. A deferred list that
    still blamed the built halves would hide that.
    """

    named = {entry["feature"] for entry in _CONTRACT["deferred"]}
    assert {
        "SRS-LOG-001-core-forwarding",
        "SRS-LOG-001-producer-coverage",
        "SRS-LOG-001-dashboard-e2e",
    } <= named
    assert not ({"SRS-UI-001", "SRS-API-001"} & named), "a built half is still listed as deferred"


def test_published_event_matches_the_declared_logs_channel_payload() -> None:
    """What the publisher emits is what the AsyncAPI document promises.

    The LOGS channel carries BOTH classes, so ``log_class`` has to be declared:
    without it a generated client has no documented discriminator and the
    system-vs-strategy separation would end at the WebSocket boundary.
    """

    import sys

    sys.path.insert(0, str(ROOT / "python"))
    from atp_logs_service import EVENT_FIELDS
    from atp_ws import EVENT_CHANNELS

    logs = next(channel for channel in EVENT_CHANNELS if channel.name.value == "LOGS")
    assert set(logs.payload_fields) == set(EVENT_FIELDS), (
        "the published LOGS event and the declared channel payload have drifted"
    )
    assert "log_class" in logs.payload_fields


def test_openapi_documents_the_real_response_types() -> None:
    """The published schema must describe what the live handler actually returns.

    Every response field defaulted to ``string`` while the route was a
    placeholder. Now that a handler serves it, a client generated from the
    document would misparse the event array, the counters, and the booleans.
    """

    import sys
    import tempfile
    from pathlib import Path as _Path

    sys.path.insert(0, str(ROOT / "python"))
    from atp_logging.persistence import build_separated_log_dispatcher
    from atp_logging.records import LogClass, LogRecord, Severity, Source
    from atp_logs_service import LogsQueryHandler
    from atp_runtime.registry import OperationKey, Request, Surface

    document = json.loads((ROOT / "python" / "atp_api" / "openapi.json").read_text())
    schema = document["paths"]["/api/v1/logs"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]

    with tempfile.TemporaryDirectory() as tmp:
        directory = _Path(tmp)
        dispatcher, system_store, _strategy = build_separated_log_dispatcher(directory)
        dispatcher.dispatch(
            LogRecord(
                timestamp_ns=1_700_000_000_000_000_000,
                severity=Severity.CRITICAL,
                source=Source.KILL_SWITCH,
                event_type="ACTIVATION",
                message="documented shape",
                correlation_id="doc-1",
                log_class=LogClass.SYSTEM,
            )
        )
        system_store.close()
        handler = LogsQueryHandler(
            system_store_path=directory / "system.jsonl",
            strategy_store_path=directory / "strategy.jsonl",
        )
        body = handler.handle(
            Request(
                surface=Surface.REST,
                operation=OperationKey(Surface.REST, "GET /api/v1/logs"),
                method="GET",
                path="/api/v1/logs",
                query={},
            )
        ).body

    json_type = {
        bool: "boolean",  # before int — bool IS an int in Python
        int: "integer",
        str: "string",
        list: "array",
        dict: "object",
    }

    def kind_of(value: object) -> str:
        if value is None:
            return "null"
        return next(k for python_type, k in json_type.items() if isinstance(value, python_type))

    def documented_types(spec: dict) -> set[str]:
        declared = spec["type"]
        return set(declared) if isinstance(declared, list) else {declared}

    # TOP-LEVEL: documented set == emitted set, and the types agree.
    assert set(schema) == set(body), (
        f"top-level schema and body disagree: only-schema={sorted(set(schema) - set(body))}, "
        f"only-body={sorted(set(body) - set(schema))}"
    )
    for name, value in body.items():
        assert kind_of(value) in documented_types(schema[name]), (
            f"/api/v1/logs documents {name!r} as {schema[name]['type']!r} "
            f"but returns {kind_of(value)!r}"
        )

    # NESTED: the per-event fields belong under events.items, NOT beside the
    # array. A client generated from a hoisted schema looks in the wrong place.
    item_schema = schema["events"]["items"]["properties"]
    event = body["events"][0]
    assert set(item_schema) == set(event), (
        f"events.items and the emitted event disagree: "
        f"only-schema={sorted(set(item_schema) - set(event))}, "
        f"only-event={sorted(set(event) - set(item_schema))}"
    )
    for name, value in event.items():
        # ``strategy_id`` is null on a system record and a string on a strategy
        # one, so its documented type is a union — a caller must be told both.
        assert kind_of(value) in documented_types(item_schema[name]), (
            f"events[].{name} documents {item_schema[name]['type']!r} "
            f"but returns {kind_of(value)!r}"
        )
    for per_event in event:
        assert per_event not in schema or per_event == "log_class", (
            f"{per_event!r} is documented as a TOP-LEVEL response field but the handler "
            "returns it inside each events[] element"
        )


def test_operator_surface_declares_the_shipped_modules() -> None:
    surface = _CONTRACT["operator_surface"]
    for key in (
        "module_path",
        "publisher_module_path",
        "wiring_module_path",
        "dashboard_provider_module",
    ):
        assert (ROOT / surface[key]).exists(), f"{key} points at a missing file"
    assert surface["rest_operation"] == "GET /api/v1/logs"
    assert surface["cli_operation"] == "admin logs"
    assert surface["ws_channel"] == "LOGS"
