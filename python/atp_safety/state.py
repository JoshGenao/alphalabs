"""Durable last-activation record — the kill switch's replay guard.

One JSON file, written with the repo's durable-persistence convention
(scratch file + ``fsync`` + atomic ``os.replace`` + parent-directory
``fsync`` — the ``JsonlLogStore`` / ``backtest_store`` pattern): a crash
mid-write leaves either the previous record or the new one, never a torn
file. A second ``kill-switch activate`` finds the record and REPLAYS it
(same ``activation_id``, no second backend call) — re-running the liquidate
sequence against already-liquidated state would re-submit market orders.

Reads fail closed: a corrupt or non-object record raises
:class:`LastActivationCorruptError` rather than being treated as "never
activated" — pretending an activation never happened is exactly the replay
the guard exists to stop. A genuinely missing file (never activated) returns
``None``.

Honest scope: this guards replays through THIS operator layer's state
directory. A cross-process lockout below the operator layer is deferred
(``kill_switch_activation_contract.deferred[]``).

Schema evolution (SRS-DATA-015 / SyRS SYS-66): the record carries a
``schema_version`` key so a reader can tell which layout it is looking at.
A record written *before* that key existed is read as
:data:`MIN_SUPPORTED_STATE_SCHEMA_VERSION` and stays usable exactly where
it lies — no migration pass, no rewrite, because rewriting a replay guard
to read it would itself be a write to the artefact whose integrity the
guard depends on. A record from a NEWER build fails closed: an activation
this build cannot parse is not an activation it may report as absent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

_STATE_FILENAME = "kill_switch_last_activation.json"

#: The record layout this build WRITES. **Version history:** v1 = the
#: ``activation_id`` / ``report`` / ``response`` record.
STATE_SCHEMA_VERSION = 1

#: The oldest record layout this build still READS. A record with no
#: ``schema_version`` key predates SRS-DATA-015 and is read at this floor.
MIN_SUPPORTED_STATE_SCHEMA_VERSION = 1

#: The key the version travels under, inside the record object itself (the
#: record is one JSON object, so there is nowhere else to put it that would
#: survive the atomic replace as one unit).
SCHEMA_VERSION_KEY = "schema_version"


class LastActivationCorruptError(Exception):
    """The persisted last-activation record exists but cannot be trusted."""


def _state_path(state_dir: Path) -> Path:
    return state_dir / _STATE_FILENAME


def persist_last_activation(state_dir: Path, payload: Mapping[str, object]) -> Path:
    """Durably persist ``payload`` as the last-activation record.

    The state directory must already exist — a missing directory is a
    misconfigured composition and fails closed rather than being silently
    created somewhere unintended.

    The writer owns the format, so it stamps :data:`STATE_SCHEMA_VERSION`
    onto the record (overriding any ``schema_version`` a caller supplied —
    a caller cannot declare a layout it does not control).
    """

    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        raise FileNotFoundError(f"kill-switch state directory does not exist: {state_dir}")
    final_path = _state_path(state_dir)
    scratch_path = final_path.with_name(f".{final_path.name}.tmp.{os.getpid()}")
    versioned = {**dict(payload), SCHEMA_VERSION_KEY: STATE_SCHEMA_VERSION}
    encoded = json.dumps(versioned, sort_keys=True).encode("utf-8")
    file_descriptor = os.open(scratch_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch_path, final_path)
    except BaseException:
        scratch_path.unlink(missing_ok=True)
        raise
    directory_descriptor = os.open(state_dir, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return final_path


def load_last_activation(state_dir: Path) -> dict[str, object] | None:
    """Return the persisted last-activation record, or ``None`` if absent.

    A present-but-unreadable record fails CLOSED
    (:class:`LastActivationCorruptError`): treating corruption as "never
    activated" would let a repeat activation re-run the liquidate sequence.
    That includes a record whose declared ``schema_version`` this build does
    not support — an unparseable activation is still an activation.

    A record with **no** ``schema_version`` predates SRS-DATA-015 and is
    read at :data:`MIN_SUPPORTED_STATE_SCHEMA_VERSION`, unchanged on disk.
    """

    path = _state_path(Path(state_dir))
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise LastActivationCorruptError(
            f"last-activation record at {path} is not valid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise LastActivationCorruptError(
            f"last-activation record at {path} must be a JSON object; got {type(payload).__name__}"
        )
    _check_schema_version(payload, path)
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``json`` ``object_pairs_hook`` that refuses a duplicated key.

    Python's default is last-value-wins, which silently resolves an ambiguity
    that must not be resolved: a record declaring both ``"schema_version":99``
    and ``"schema_version":1`` would be read as v1 and served, when what it
    actually says is that this build cannot trust it. Mirrors the Rust
    ``atp_types::json_scan`` gate, so the two languages agree about which
    persisted records are readable.
    """

    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


def _check_schema_version(payload: Mapping[str, object], path: Path) -> None:
    """Fail closed unless this build can read ``payload``'s declared layout.

    Absent key → a pre-SRS-DATA-015 record, read at the supported floor.
    Present → must be a real ``int`` (``bool`` is an ``int`` subclass in
    Python and is rejected, as is a numeric string: a version that arrived
    as the wrong type means the record is not the shape it claims) within
    ``[MIN_SUPPORTED_STATE_SCHEMA_VERSION, STATE_SCHEMA_VERSION]``.
    """

    if SCHEMA_VERSION_KEY not in payload:
        return
    version = payload[SCHEMA_VERSION_KEY]
    if isinstance(version, bool) or not isinstance(version, int):
        raise LastActivationCorruptError(
            f"last-activation record at {path} declares a non-integer "
            f"{SCHEMA_VERSION_KEY} ({version!r}) — refusing to guess its layout"
        )
    if not (MIN_SUPPORTED_STATE_SCHEMA_VERSION <= version <= STATE_SCHEMA_VERSION):
        raise LastActivationCorruptError(
            f"last-activation record at {path} declares {SCHEMA_VERSION_KEY} "
            f"{version}, outside the supported range "
            f"[{MIN_SUPPORTED_STATE_SCHEMA_VERSION}, {STATE_SCHEMA_VERSION}] — refusing to "
            "read it as never-activated"
        )
