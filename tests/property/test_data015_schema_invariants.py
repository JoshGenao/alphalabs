"""L2 property tests for SRS-DATA-015 version gates (Hypothesis).

The unit and domain tests pin specific versions. These properties quantify over the whole integer
domain instead, so the gates cannot be right only for the handful of values someone thought to type:

* **inside the supported range → accepted**, for every version in it;
* **outside it → rejected**, for every other integer — in particular every *future* version, which
  is the direction that matters (guessing at an unseen layout is how a reader silently misreports
  data it cannot actually parse);
* **non-integer versions → rejected**, including the ``bool`` that Python's type system makes an
  ``int`` subclass and the numeric string that JSON round-trips make easy to introduce.

All three retrofitted Python readers are held to the same properties, because a gate that behaves
differently per artefact is a gate someone will reason about wrongly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_logging.persistence import (  # noqa: E402
    MIN_SUPPORTED_SEGMENT_SCHEMA_VERSION,
    SCHEMA_VERSION_KEY,
    SEGMENT_SCHEMA_VERSION,
    LogStoreCorruptionError,
    read_records,
)
from atp_logging.records import LogClass, LogRecord, Severity, Source  # noqa: E402
from atp_readiness.probes import (  # noqa: E402
    ALERT_SINK_SCHEMA_VERSION,
    MIN_SUPPORTED_ALERT_SINK_SCHEMA_VERSION,
    AlertSinkSchemaError,
    JsonlAlertSink,
)
from atp_safety.state import (  # noqa: E402
    MIN_SUPPORTED_STATE_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    LastActivationCorruptError,
    load_last_activation,
)

pytestmark = [pytest.mark.property]

# Version values that are not integers at all. ``True`` is included deliberately: it is an ``int``
# subclass in Python and would pass a naive ``isinstance(v, int)`` check as version 1.
NON_INTEGER_VERSIONS = st.sampled_from([True, False, "1", "", None, 1.0, 1.5, [], {}])

#: Distinct from ``None``: the helpers below need to express "omit the key entirely" separately from
#: "the key is present and holds JSON ``null``". ``None`` is itself a value under test (an explicit
#: null version must be rejected), so it cannot double as the absent-key sentinel.
ABSENT = object()


def _activation(version: object = ABSENT) -> dict[str, object]:
    record: dict[str, object] = {
        "activation_id": "act-prop",
        "report": {"activation_id": "act-prop", "ok": True},
        "response": {"activation_id": "act-prop", "accepted": True},
    }
    if version is not ABSENT:
        record[SCHEMA_VERSION_KEY] = version
    return record


def _log_line(version: object = ABSENT) -> str:
    record = LogRecord(
        timestamp_ns=1_700_000_000_000_000_000,
        severity=Severity.INFO,
        source=Source.IB_GATEWAY,
        event_type="CONNECT",
        message="property line",
        correlation_id="corr-prop",
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    ).as_dict()
    if version is not ABSENT:
        record[SCHEMA_VERSION_KEY] = version
    return json.dumps(record) + "\n"


def _alert_line(version: object = ABSENT) -> str:
    record: dict[str, object] = {
        "detail": "property alert",
        "kind": "DEGRADED",
        "observed_at_ns": 1_700_000_000_000_000_000,
        "srs_trace": ["SRS-MD-006"],
        "subject": "readiness",
    }
    if version is not ABSENT:
        record[SCHEMA_VERSION_KEY] = version
    return json.dumps(record, sort_keys=True) + "\n"


# --------------------------------------------------------------------------- #
# Kill-switch replay guard
# --------------------------------------------------------------------------- #


@given(
    version=st.integers(
        min_value=MIN_SUPPORTED_STATE_SCHEMA_VERSION, max_value=STATE_SCHEMA_VERSION
    )
)
def test_every_supported_activation_version_is_accepted(tmp_path_factory, version: int) -> None:
    directory = tmp_path_factory.mktemp("state-ok")
    (directory / "kill_switch_last_activation.json").write_text(json.dumps(_activation(version)))
    loaded = load_last_activation(directory)
    assert loaded is not None
    assert loaded["activation_id"] == "act-prop"


@given(version=st.integers().filter(lambda v: not (1 <= v <= STATE_SCHEMA_VERSION)))
def test_every_unsupported_activation_version_fails_closed(tmp_path_factory, version: int) -> None:
    directory = tmp_path_factory.mktemp("state-bad")
    (directory / "kill_switch_last_activation.json").write_text(json.dumps(_activation(version)))
    with pytest.raises(LastActivationCorruptError):
        load_last_activation(directory)


@given(version=NON_INTEGER_VERSIONS)
def test_a_non_integer_activation_version_fails_closed(tmp_path_factory, version: object) -> None:
    directory = tmp_path_factory.mktemp("state-type")
    (directory / "kill_switch_last_activation.json").write_text(json.dumps(_activation(version)))
    with pytest.raises(LastActivationCorruptError):
        load_last_activation(directory)


# --------------------------------------------------------------------------- #
# SYSTEM log segment
# --------------------------------------------------------------------------- #


@given(
    version=st.integers(
        min_value=MIN_SUPPORTED_SEGMENT_SCHEMA_VERSION, max_value=SEGMENT_SCHEMA_VERSION
    )
)
def test_every_supported_log_version_is_readable(tmp_path_factory, version: int) -> None:
    path = tmp_path_factory.mktemp("log-ok") / "system.jsonl"
    path.write_text(_log_line(version))
    assert len(read_records(path)) == 1


@given(version=st.integers().filter(lambda v: not (1 <= v <= SEGMENT_SCHEMA_VERSION)))
def test_every_unsupported_log_version_is_corruption(tmp_path_factory, version: int) -> None:
    path = tmp_path_factory.mktemp("log-bad") / "system.jsonl"
    path.write_text(_log_line(version))
    with pytest.raises(LogStoreCorruptionError):
        read_records(path)


@given(version=NON_INTEGER_VERSIONS)
def test_a_non_integer_log_version_is_corruption(tmp_path_factory, version: object) -> None:
    path = tmp_path_factory.mktemp("log-type") / "system.jsonl"
    path.write_text(_log_line(version))
    with pytest.raises(LogStoreCorruptionError):
        read_records(path)


# --------------------------------------------------------------------------- #
# Readiness alert sink
# --------------------------------------------------------------------------- #


@given(
    version=st.integers(
        min_value=MIN_SUPPORTED_ALERT_SINK_SCHEMA_VERSION, max_value=ALERT_SINK_SCHEMA_VERSION
    )
)
def test_every_supported_alert_version_is_readable(tmp_path_factory, version: int) -> None:
    path = tmp_path_factory.mktemp("alert-ok") / "alerts.jsonl"
    path.write_text(_alert_line(version))
    assert len(JsonlAlertSink(path).read()) == 1


@given(version=st.integers().filter(lambda v: not (1 <= v <= ALERT_SINK_SCHEMA_VERSION)))
def test_every_unsupported_alert_version_fails_closed(tmp_path_factory, version: int) -> None:
    path = tmp_path_factory.mktemp("alert-bad") / "alerts.jsonl"
    path.write_text(_alert_line(version))
    with pytest.raises(AlertSinkSchemaError):
        JsonlAlertSink(path).read()


@given(version=NON_INTEGER_VERSIONS)
def test_a_non_integer_alert_version_fails_closed(tmp_path_factory, version: object) -> None:
    path = tmp_path_factory.mktemp("alert-type") / "alerts.jsonl"
    path.write_text(_alert_line(version))
    with pytest.raises(AlertSinkSchemaError):
        JsonlAlertSink(path).read()


# --------------------------------------------------------------------------- #
# The shared legacy property
# --------------------------------------------------------------------------- #


def test_a_version_less_payload_is_readable_for_every_retrofitted_reader(tmp_path) -> None:
    """The single rule all three readers share: no version key → read at the floor.

    Stated once, over all three, because the AC's "without bulk migration" is only true if it holds
    for every entity that has files already on disk.
    """

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "kill_switch_last_activation.json").write_text(json.dumps(_activation(ABSENT)))
    assert load_last_activation(state_dir) is not None

    log_path = tmp_path / "system.jsonl"
    log_path.write_text(_log_line(ABSENT))
    assert len(read_records(log_path)) == 1

    alert_path = tmp_path / "alerts.jsonl"
    alert_path.write_text(_alert_line(ABSENT))
    assert len(JsonlAlertSink(alert_path).read()) == 1
