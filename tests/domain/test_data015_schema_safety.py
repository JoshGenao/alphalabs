"""L7 domain test for SRS-DATA-015 on the SAFETY-path persisted entities.

SRS-DATA-015 added a ``schema_version`` to two artefacts that gate trading
safety, so the schema gate itself becomes a safety mechanism and is tested as
one:

* ``python/atp_safety/state.py`` — the kill-switch **replay guard**. A second
  ``kill-switch activate`` reads this record and REPLAYS it instead of
  re-running the liquidate sequence. If a version gate ever let this record
  read as *absent*, a repeat activation would re-submit market orders against
  already-liquidated positions. So the safety-relevant assertion is not "the
  version parses" but "**no** version outcome is ever silently treated as
  never-activated".

* ``python/atp_logging/persistence.py`` — the SYSTEM audit trail the SYS-44b
  liquidation-timeout evidence is read back from. A line this build cannot
  parse must fail closed rather than be served as an audit record.

The same rule is exercised on a third safety-relevant artefact, the SRS-RESV-003
hot-swap trigger log: its record COUNT is the evidence behind "all swap triggers
are logged", so a line whose schema version this build cannot parse must not be
counted as proof that a trigger was durably recorded.

The direction of failure is what matters here: for all three artefacts the safe
answer to "I cannot read this" is **refuse**, never "nothing was recorded".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_logging.persistence import (  # noqa: E402
    MIN_SUPPORTED_SEGMENT_SCHEMA_VERSION,
    SCHEMA_VERSION_KEY,
    SEGMENT_SCHEMA_VERSION,
    JsonlLogStore,
    LogStoreCorruptionError,
    read_records,
)
from atp_logging.records import LogClass, LogRecord, Severity, Source  # noqa: E402
from atp_readiness.probes import (  # noqa: E402
    ALERT_SINK_SCHEMA_VERSION,
    AlertSinkSchemaError,
    JsonlAlertSink,
)
from atp_safety.state import (  # noqa: E402
    MIN_SUPPORTED_STATE_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    LastActivationCorruptError,
    load_last_activation,
    persist_last_activation,
)

pytestmark = [pytest.mark.domain]

CORPUS = ROOT / "tests" / "fixtures" / "schema_evolution"


def _activation(activation_id: str = "act-0001") -> dict[str, object]:
    return {
        "activation_id": activation_id,
        "report": {"activation_id": activation_id, "cancelled": 3, "liquidated": 2, "ok": True},
        "response": {"activation_id": activation_id, "accepted": True},
    }


# --------------------------------------------------------------------------- #
# Kill-switch replay guard
# --------------------------------------------------------------------------- #


def test_persisted_activation_records_its_schema_version(tmp_path: Path) -> None:
    """AC clause 1 on the safety artefact: the version reaches the bytes."""

    persist_last_activation(tmp_path, _activation())
    raw = json.loads((tmp_path / "kill_switch_last_activation.json").read_text())
    assert raw[SCHEMA_VERSION_KEY] == STATE_SCHEMA_VERSION


def test_a_legacy_version_less_activation_still_guards_the_replay(tmp_path: Path) -> None:
    """AC clause 2 on the safety artefact — and the safety property itself.

    A record written before SRS-DATA-015 carries no version. It MUST still read
    back as a recorded activation: reading it as ``None`` would let a second
    activate re-run the liquidate sequence against already-flat positions.
    """

    legacy_bytes = (CORPUS / "kill_switch_last_activation_legacy.json").read_bytes()
    target = tmp_path / "kill_switch_last_activation.json"
    target.write_bytes(legacy_bytes)

    loaded = load_last_activation(tmp_path)
    assert loaded is not None, "a legacy activation record must never read as never-activated"
    assert loaded["activation_id"] == "act-legacy-0001"
    assert SCHEMA_VERSION_KEY not in loaded, "the legacy record is read as written, not upgraded"
    # ...and reading it did not rewrite the guard's own artefact.
    assert target.read_bytes() == legacy_bytes


def test_an_unknown_future_activation_version_fails_closed(tmp_path: Path) -> None:
    """A record from a NEWER build must raise, NOT read as absent.

    This is the whole safety argument for the gate: ``None`` means "never
    activated" and unlocks a fresh liquidate sequence. A record this build
    cannot parse says nothing about whether the sequence ran, so the only safe
    answer is to refuse.
    """

    record = {**_activation(), SCHEMA_VERSION_KEY: STATE_SCHEMA_VERSION + 1}
    (tmp_path / "kill_switch_last_activation.json").write_text(json.dumps(record))

    with pytest.raises(LastActivationCorruptError) as excinfo:
        load_last_activation(tmp_path)
    assert "never-activated" in str(excinfo.value)


@pytest.mark.parametrize(
    "version",
    [
        MIN_SUPPORTED_STATE_SCHEMA_VERSION - 1,
        STATE_SCHEMA_VERSION + 1,
        "1",  # a numeric STRING is not a version
        True,  # bool is an int subclass in Python — must not pass as version 1
        None,
        1.0,  # a float that equals a supported version is still not an int
    ],
)
def test_no_unreadable_activation_version_ever_reads_as_absent(
    tmp_path: Path, version: object
) -> None:
    """Exhaustive on the failure direction: every rejected version RAISES.

    A returned ``None`` here would be the dangerous outcome, so the assertion
    is specifically that ``load_last_activation`` never takes that branch.
    """

    record = {**_activation(), SCHEMA_VERSION_KEY: version}
    (tmp_path / "kill_switch_last_activation.json").write_text(json.dumps(record))
    with pytest.raises(LastActivationCorruptError):
        load_last_activation(tmp_path)


def test_a_genuinely_absent_record_still_reads_as_none(tmp_path: Path) -> None:
    """The one legitimate ``None``: nothing was ever written.

    The gate must not turn "never activated" into an error either — that would
    block the FIRST activation, which is the one that has to work.
    """

    assert load_last_activation(tmp_path) is None


def test_a_caller_cannot_forge_the_schema_version(tmp_path: Path) -> None:
    """The writer owns the format.

    A caller passing its own ``schema_version`` must not be able to stamp the
    record as a layout it does not actually have — otherwise a mis-set field
    could make a current record unreadable to the very build that wrote it.
    """

    persist_last_activation(tmp_path, {**_activation(), SCHEMA_VERSION_KEY: 999})
    raw = json.loads((tmp_path / "kill_switch_last_activation.json").read_text())
    assert raw[SCHEMA_VERSION_KEY] == STATE_SCHEMA_VERSION
    assert load_last_activation(tmp_path) is not None


def test_round_trip_through_the_current_writer_is_readable(tmp_path: Path) -> None:
    persist_last_activation(tmp_path, _activation("act-current"))
    loaded = load_last_activation(tmp_path)
    assert loaded is not None
    assert loaded["activation_id"] == "act-current"


# --------------------------------------------------------------------------- #
# SYSTEM audit trail
# --------------------------------------------------------------------------- #


def _system_record(ts: int = 1_700_000_000_000_000_000) -> LogRecord:
    return LogRecord(
        timestamp_ns=ts,
        severity=Severity.WARN,
        source=Source.KILL_SWITCH,
        event_type="LIQUIDATION_TIMEOUT",
        message="timeout evidence",
        correlation_id="corr-1",
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )


def test_persisted_log_line_records_its_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "system.jsonl"
    with JsonlLogStore(path, log_class=LogClass.SYSTEM) as store:
        store.write(_system_record())
    line = json.loads(path.read_text().splitlines()[0])
    assert line[SCHEMA_VERSION_KEY] == SEGMENT_SCHEMA_VERSION


def test_the_sdk_record_schema_is_not_widened_by_the_storage_version() -> None:
    """The version lives in the persisted envelope, not in ``LogRecord``.

    ``as_dict()`` is the SRS-LOG-001 SDK schema shared with the API/UI sinks;
    a storage concern must not appear in it.
    """

    assert SCHEMA_VERSION_KEY not in _system_record().as_dict()


def test_a_legacy_version_less_log_segment_is_still_queryable(tmp_path: Path) -> None:
    """AC clause 2 on the audit trail: an existing log stays readable in place."""

    legacy_bytes = (CORPUS / "system_log_segment_legacy.jsonl").read_bytes()
    path = tmp_path / "system.jsonl"
    path.write_bytes(legacy_bytes)

    records = read_records(path)
    assert [r.event_type for r in records] == ["CONNECT", "LIQUIDATION_TIMEOUT"]
    assert path.read_bytes() == legacy_bytes, "reading an audit trail must not rewrite it"


def test_a_legacy_and_a_current_line_coexist_in_one_segment(tmp_path: Path) -> None:
    """Rotation-free upgrade: the running store appends to the existing file."""

    path = tmp_path / "system.jsonl"
    path.write_bytes((CORPUS / "system_log_segment_legacy.jsonl").read_bytes())
    with JsonlLogStore(path, log_class=LogClass.SYSTEM) as store:
        store.write(_system_record())

    records = read_records(path)
    assert len(records) == 3
    assert records[-1].message == "timeout evidence"


def test_an_unknown_future_log_version_fails_closed(tmp_path: Path) -> None:
    """A line from a newer build is refused, never served as an audit record."""

    path = tmp_path / "system.jsonl"
    payload = {
        **_system_record().as_dict(),
        SCHEMA_VERSION_KEY: SEGMENT_SCHEMA_VERSION + 1,
    }
    path.write_text(json.dumps(payload) + "\n")
    with pytest.raises(LogStoreCorruptionError):
        read_records(path)


@pytest.mark.parametrize(
    "version",
    [MIN_SUPPORTED_SEGMENT_SCHEMA_VERSION - 1, SEGMENT_SCHEMA_VERSION + 1, "1", True, None],
)
def test_no_unreadable_log_version_is_silently_skipped(tmp_path: Path, version: object) -> None:
    """A bad version must be corruption, not a dropped line.

    Silently skipping it would delete evidence from an audit trail — the
    failure mode ``LogStoreCorruptionError`` exists to prevent.
    """

    path = tmp_path / "system.jsonl"
    good = _system_record(ts=1)
    bad = {**_system_record(ts=2).as_dict(), SCHEMA_VERSION_KEY: version}
    path.write_text(
        json.dumps({**good.as_dict(), SCHEMA_VERSION_KEY: SEGMENT_SCHEMA_VERSION})
        + "\n"
        + json.dumps(bad)
        + "\n"
    )
    with pytest.raises(LogStoreCorruptionError):
        read_records(path)


def test_the_version_gate_does_not_weaken_the_existing_invariant_checks(tmp_path: Path) -> None:
    """A correctly-versioned but invariant-violating line is still corruption.

    The version gate must not become an escape hatch: stamping the current
    version onto a SYSTEM line that carries a ``strategy_id`` must not make it
    readable.
    """

    path = tmp_path / "system.jsonl"
    payload = {
        **_system_record().as_dict(),
        "strategy_id": "leaked",
        SCHEMA_VERSION_KEY: SEGMENT_SCHEMA_VERSION,
    }
    path.write_text(json.dumps(payload) + "\n")
    with pytest.raises(LogStoreCorruptionError):
        read_records(path)


# --------------------------------------------------------------------------- #
# SRS-RESV-003 hot-swap trigger log — audit evidence cannot be manufactured
# --------------------------------------------------------------------------- #

TRIGGER_CLI = ROOT / "target" / "debug" / "resv003_hot_swap_trigger_cli"

pytestmark_needs_cli = pytest.mark.skipif(
    not TRIGGER_CLI.exists(),
    reason="resv003_hot_swap_trigger_cli not built (cargo build -p atp-orchestrator --bins)",
)


def _manual_over_seeded_log(tmp_path: Path, seed: str) -> subprocess.CompletedProcess[str]:
    """Seed a trigger log, then append to it with a real ``manual`` invocation.

    The append re-reads the whole file to count records, so a seeded line this
    build cannot parse surfaces as a non-zero exit.
    """

    log = tmp_path / "triggers.jsonl"
    log.write_text(seed, encoding="utf-8")
    return subprocess.run(
        [
            str(TRIGGER_CLI),
            "manual",
            "--demoting",
            "live-a",
            "--candidate",
            "cand-b",
            "--log",
            str(log),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytestmark_needs_cli
def test_a_legacy_trigger_log_still_accepts_appends(tmp_path: Path) -> None:
    """An audit trail written before SRS-DATA-015 must stay usable.

    Refusing it would strand every existing log — the opposite failure from the
    one below, and just as wrong.
    """

    result = _manual_over_seeded_log(
        tmp_path,
        '{"kind":"MANUAL_PROMOTION","demoting_strategy_id":"a",'
        '"candidate_strategy_id":"b","rationale":"x","observed_at_seconds":1}\n',
    )
    assert result.returncode == 0, result.stderr


@pytestmark_needs_cli
@pytest.mark.parametrize(
    "seed",
    [
        '{"schema_version":99,"kind":"X"}\n',  # future version, key first
        '{"kind":"X","schema_version":99,"rationale":"y"}\n',  # future version, key reordered
        '{"schema_version":1.5,"kind":"X"}\n',  # not an integer
        '{"schema_version":"1","kind":"X"}\n',  # quoted number
        '{"schema_version":true,"kind":"X"}\n',  # bool
        '{"schema_version":null,"kind":"X"}\n',  # null
        "this is not a json object at all\n",  # unparseable
    ],
)
def test_an_unreadable_trigger_record_is_never_counted_as_evidence(
    tmp_path: Path, seed: str
) -> None:
    """The safety property: unparseable bytes must not become audit evidence.

    The record count is what backs the CLI's "all swap triggers are logged"
    check. If a line this build cannot parse were counted — or silently
    downgraded to "legacy" — the command would report a Hot-Swap trigger as
    durably logged on the strength of bytes it does not understand, and the
    RESV-004 gate downstream would act on that claim.
    """

    result = _manual_over_seeded_log(tmp_path, seed)
    assert result.returncode != 0, f"{seed!r} must be refused, not counted"
    assert "cannot read" in result.stderr


@pytestmark_needs_cli
def test_a_version_shaped_rationale_cannot_spoof_a_version(tmp_path: Path) -> None:
    """``rationale`` is operator-supplied text.

    A substring search for the key would read a version out of it and refuse a
    perfectly good legacy record — a false alarm that would block a real
    Hot-Swap on cosmetic grounds.
    """

    result = _manual_over_seeded_log(
        tmp_path,
        '{"kind":"MANUAL_PROMOTION","demoting_strategy_id":"a","candidate_strategy_id":"b",'
        '"rationale":"note: \\"schema_version\\":99 appears here","observed_at_seconds":1}\n',
    )
    assert result.returncode == 0, result.stderr


@pytestmark_needs_cli
def test_the_writer_stamps_every_trigger_line(tmp_path: Path) -> None:
    result = _manual_over_seeded_log(tmp_path, "")
    assert result.returncode == 0, result.stderr
    written = (tmp_path / "triggers.jsonl").read_text(encoding="utf-8")
    assert written.startswith('{"schema_version":1,')


@pytestmark_needs_cli
@pytest.mark.parametrize(
    "seed",
    [
        '{"schema_version":1,\n',  # torn line: fields never arrived
        '{"schema_version":1\n',  # no closing brace
        '{"schema_version":1}trailing\n',  # trailing bytes after the object
        '{"schema_version":1,"schema_version":1}\n',  # duplicated declaration
        '{"schema_version":1,"kind":}\n',  # malformed value later in the object
    ],
)
def test_malformation_after_a_valid_version_is_still_refused(tmp_path: Path, seed: str) -> None:
    """A supported version early in the line does not make the rest of it real.

    The version gate must validate the WHOLE record before believing the version
    it found. Returning as soon as the key matched would let a torn trigger
    record — one whose remaining fields never arrived — be counted as proof that
    a Hot-Swap trigger was durably logged, which is the exact false assurance
    ``count_log_records`` exists to prevent.
    """

    result = _manual_over_seeded_log(tmp_path, seed)
    assert result.returncode != 0, f"{seed!r} must be refused despite a parseable version"
    assert "cannot read" in result.stderr


@pytestmark_needs_cli
def test_a_supported_version_alone_is_not_a_trigger_record(tmp_path: Path) -> None:
    """A known layout is not the same as a present record.

    ``{"schema_version":1}`` declares a layout this build understands and
    carries no trigger at all. Counting it would let an empty object stand in
    for durable proof that a Hot-Swap trigger was logged — the RESV-004 gate
    downstream acts on that count.
    """

    result = _manual_over_seeded_log(tmp_path, '{"schema_version":1}\n')
    assert result.returncode != 0
    assert "cannot read" in result.stderr


@pytestmark_needs_cli
@pytest.mark.parametrize(
    "dropped",
    [
        "kind",
        "demoting_strategy_id",
        "candidate_strategy_id",
        "rationale",
        "observed_at_seconds",
    ],
)
def test_a_trigger_record_missing_a_required_field_is_not_evidence(
    tmp_path: Path, dropped: str
) -> None:
    """Every field the event needs must be there, or the record proves nothing."""

    full = {
        "schema_version": 1,
        "kind": "MANUAL_PROMOTION",
        "demoting_strategy_id": "live-a",
        "candidate_strategy_id": "cand-b",
        "rationale": "manual",
        "observed_at_seconds": 1_715_000_000,
    }
    del full[dropped]
    result = _manual_over_seeded_log(tmp_path, json.dumps(full) + "\n")
    assert result.returncode != 0, f"missing {dropped} must be refused"
    assert "cannot read" in result.stderr


@pytestmark_needs_cli
def test_a_complete_legacy_trigger_record_is_still_evidence(tmp_path: Path) -> None:
    """The strictness must not start refusing real pre-SRS-DATA-015 records."""

    legacy = {
        "kind": "MANUAL_PROMOTION",
        "demoting_strategy_id": "live-a",
        "candidate_strategy_id": "cand-b",
        "rationale": "manual",
        "observed_at_seconds": 1_715_000_000,
    }
    result = _manual_over_seeded_log(tmp_path, json.dumps(legacy) + "\n")
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# Duplicate-key ambiguity — all three Python persisted readers must agree
# --------------------------------------------------------------------------- #


def test_a_duplicated_schema_version_is_refused_by_every_python_reader(tmp_path: Path) -> None:
    """Ambiguity must be refused, not resolved by key order.

    Python's ``json`` is last-value-wins, so a record declaring both
    ``schema_version: 99`` and ``schema_version: 1`` would be read as v1 and
    served — exactly inverting the fail-closed contract, because the record's
    own first claim is that this build cannot read it. Mirrors the Rust
    ``atp_types::json_scan`` duplicate-key gate so the two languages agree.
    """

    future, current = SEGMENT_SCHEMA_VERSION + 98, SEGMENT_SCHEMA_VERSION

    # 1. SYSTEM log segment.
    log_path = tmp_path / "system.jsonl"
    body = json.dumps(_system_record().as_dict())[1:-1]
    log_path.write_text(
        f'{{"{SCHEMA_VERSION_KEY}":{future},{body},"{SCHEMA_VERSION_KEY}":{current}}}\n'
    )
    with pytest.raises(LogStoreCorruptionError):
        read_records(log_path)

    # 2. Kill-switch last-activation record.
    state_path = tmp_path / "kill_switch_last_activation.json"
    activation_body = json.dumps(_activation())[1:-1]
    state_path.write_text(
        f'{{"{SCHEMA_VERSION_KEY}":{STATE_SCHEMA_VERSION + 98},{activation_body},'
        f'"{SCHEMA_VERSION_KEY}":{STATE_SCHEMA_VERSION}}}'
    )
    with pytest.raises(LastActivationCorruptError):
        load_last_activation(tmp_path)

    # 3. Readiness alert sink.
    alert_path = tmp_path / "alerts.jsonl"
    alert_path.write_text(
        f'{{"{SCHEMA_VERSION_KEY}":{ALERT_SINK_SCHEMA_VERSION + 98},"subject":"readiness",'
        f'"{SCHEMA_VERSION_KEY}":{ALERT_SINK_SCHEMA_VERSION}}}\n'
    )
    with pytest.raises(AlertSinkSchemaError):
        JsonlAlertSink(alert_path).read()


def test_a_duplicated_ordinary_key_is_also_refused(tmp_path: Path) -> None:
    """The rule is about ambiguity, not about the version key specifically.

    A record that says two different things about ANY field is a record whose
    contents this build cannot state, and an audit trail may not serve one.
    """

    log_path = tmp_path / "system.jsonl"
    body = json.dumps({**_system_record().as_dict(), SCHEMA_VERSION_KEY: SEGMENT_SCHEMA_VERSION})
    log_path.write_text(body[:-1] + ',"message":"a second, different message"}\n')
    with pytest.raises(LogStoreCorruptionError):
        read_records(log_path)


@pytestmark_needs_cli
@pytest.mark.parametrize(
    ("tag", "strategy_id"),
    [
        ("backspace", "live-" + chr(0x08) + "a"),
        ("form-feed", "live-" + chr(0x0C) + "a"),
        ("unit-separator", "live-" + chr(0x1F) + "a"),
        ("bell", "live-" + chr(0x07) + "a"),
        ("newline", "live-" + chr(0x0A) + "a"),
        ("quote", 'live-"a'),
        ("backslash", "live-" + chr(0x5C) + "a"),
    ],
)
def test_a_control_character_in_an_id_cannot_poison_the_audit_log(
    tmp_path: Path, tag: str, strategy_id: str
) -> None:
    """Data this build WRITES must stay readable by this build.

    Strategy ids are operator-supplied and the trigger log is hand-serialised.
    If the writer emits a raw control character that the reader (correctly)
    refuses, the log becomes unreadable from that record on: every later
    trigger loses its evidence, and the "all swap triggers are logged" claim
    silently stops being true. Two passes -- the second re-reads and counts
    what the first wrote, so a round-trip failure shows up as a non-zero exit.
    """

    log = tmp_path / "triggers.jsonl"
    for attempt in range(2):
        result = subprocess.run(
            [
                str(TRIGGER_CLI),
                "manual",
                "--demoting",
                strategy_id,
                "--candidate",
                "cand-b",
                "--log",
                str(log),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{tag} pass {attempt}: {result.stderr}"

    contents = log.read_text(encoding="utf-8")
    assert len([line for line in contents.splitlines() if line.strip()]) == 2
    assert not any(ord(c) < 0x20 and c != chr(0x0A) for c in contents), (
        f"{tag}: a raw control character reached the durable audit log"
    )


# --------------------------------------------------------------------------- #
# Credential vault — the fourth Python persisted reader
# --------------------------------------------------------------------------- #


def test_the_vault_refuses_an_ambiguous_envelope_version(tmp_path: Path) -> None:
    """Every Python persisted reader answers "is this readable?" the same way.

    The vault has always WRITTEN a ``version``; SRS-DATA-015 gave it the
    matching read gate. That gate is only worth anything if the parse beneath
    it cannot be steered: with last-value-wins JSON, an envelope declaring an
    unsupported future ``version`` followed by a current one would be opened as
    current — and for a credential store, opening under the wrong assumed
    layout means deriving a key from fields that may not mean what this build
    thinks they mean.
    """

    from atp_config.vault import CredentialVault, VaultFormatError, generate_key

    key = generate_key()
    path = tmp_path / "vault.json"
    CredentialVault(path, key=key).seal({"ATP_IB_ACCOUNT": "DU123456"})

    # Sanity: the sealed vault opens normally.
    assert CredentialVault(path, key=key).open() == {"ATP_IB_ACCOUNT": "DU123456"}

    envelope = json.loads(path.read_text())
    body = json.dumps(envelope)[1:-1]
    path.write_text('{"version":99,' + body + "}")

    with pytest.raises(VaultFormatError):
        CredentialVault(path, key=key).open()


def test_the_vault_refuses_an_unsupported_envelope_version(tmp_path: Path) -> None:
    """A vault sealed by a NEWER build is refused, not mis-parsed.

    Without the gate this surfaced as a misleading "wrong key" decrypt failure,
    because the ``kdf``/``scrypt`` header decides how the key is derived.
    """

    from atp_config.vault import CredentialVault, VaultFormatError, generate_key

    key = generate_key()
    path = tmp_path / "vault.json"
    CredentialVault(path, key=key).seal({"ATP_IB_ACCOUNT": "DU123456"})

    envelope = json.loads(path.read_text())
    envelope["version"] = 99
    path.write_text(json.dumps(envelope))

    with pytest.raises(VaultFormatError) as excinfo:
        CredentialVault(path, key=key).open()
    assert "newer build" in str(excinfo.value)
