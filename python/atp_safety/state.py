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
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

_STATE_FILENAME = "kill_switch_last_activation.json"


class LastActivationCorruptError(Exception):
    """The persisted last-activation record exists but cannot be trusted."""


def _state_path(state_dir: Path) -> Path:
    return state_dir / _STATE_FILENAME


def persist_last_activation(state_dir: Path, payload: Mapping[str, object]) -> Path:
    """Durably persist ``payload`` as the last-activation record.

    The state directory must already exist — a missing directory is a
    misconfigured composition and fails closed rather than being silently
    created somewhere unintended.
    """

    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        raise FileNotFoundError(f"kill-switch state directory does not exist: {state_dir}")
    final_path = _state_path(state_dir)
    scratch_path = final_path.with_name(f".{final_path.name}.tmp.{os.getpid()}")
    encoded = json.dumps(dict(payload), sort_keys=True).encode("utf-8")
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
    """

    path = _state_path(Path(state_dir))
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LastActivationCorruptError(
            f"last-activation record at {path} is not valid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise LastActivationCorruptError(
            f"last-activation record at {path} must be a JSON object; got {type(payload).__name__}"
        )
    return payload
