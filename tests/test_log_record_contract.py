"""L3 contract tests for the SRS-LOG-001 SDK-surface log record + dispatcher.

Three layers of evidence:

* :class:`LogRecordScriptTest` runs ``tools/log_record_check.py`` as a
  subprocess so the positive-evidence path stays under CI coverage.
* :class:`LogRecordMutationTest` rebuilds ``python/atp_logging`` in a
  temporary copy of the repo, mutates one rule, and re-runs the check —
  ensuring each rule has an L3 negative-case anchor.
* :class:`LogRecordContractBlockParityTest` cross-checks the contract block
  against the Python implementation without a subprocess (pure assertions
  over the JSON + the imported package).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_logging import (  # noqa: E402  (path manipulation must come first)
    EVENT_TYPES_BY_SOURCE,
    STRATEGY_SOURCES,
    SYSTEM_SOURCES,
    LogClass,
    LogClassError,
    LogPayloadError,
    LogRecord,
    LogRoutingError,
    LogSinkError,
    RoutedLogDispatcher,
    Severity,
    Source,
    is_finite_non_negative_int,
)


def _system_record(**overrides: object) -> LogRecord:
    kwargs: dict[str, object] = dict(
        timestamp_ns=time.time_ns(),
        severity=Severity.INFO,
        source=Source.KILL_SWITCH,
        event_type="ACTIVATION",
        message="ok",
        correlation_id="c",
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )
    kwargs.update(overrides)
    return LogRecord(**kwargs)  # type: ignore[arg-type]


def _strategy_record(**overrides: object) -> LogRecord:
    kwargs: dict[str, object] = dict(
        timestamp_ns=time.time_ns(),
        severity=Severity.INFO,
        source=Source.STRATEGY,
        event_type="user_signal",
        message="ok",
        correlation_id="c",
        log_class=LogClass.STRATEGY,
        strategy_id="strat-1",
    )
    kwargs.update(overrides)
    return LogRecord(**kwargs)  # type: ignore[arg-type]


class _CapturingSink:
    def __init__(self) -> None:
        self.records: list[LogRecord] = []

    def write(self, record: LogRecord) -> None:
        self.records.append(record)


# --------------------------------------------------------------------------- #
# Subprocess positive-evidence
# --------------------------------------------------------------------------- #


class LogRecordScriptTest(unittest.TestCase):
    def test_script_returns_zero_and_prints_sdk_surface_pass(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/log_record_check.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("SRS-LOG-001 SDK-SURFACE PASS", result.stdout)
        # The persistent-sink runtime half is now BUILT (atp_logging.persistence);
        # the PASS line points at it and still names the still-deferred downstream
        # owners (dashboard SRS-UI-001, live REST/WS/CLI SRS-API-001, notify fan-out
        # SRS-NOTIF-001).
        self.assertIn("atp_logging.persistence", result.stdout)
        for owner in ("SRS-UI-001", "SRS-API-001", "SRS-NOTIF-001"):
            self.assertIn(owner, result.stdout, f"PASS line should mention {owner}")


# --------------------------------------------------------------------------- #
# Dispatcher behavioural exercise (covers happy + sad paths)
# --------------------------------------------------------------------------- #


class DispatcherBehaviourTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dispatcher = RoutedLogDispatcher()
        self.system_sink = _CapturingSink()
        self.strategy_sink = _CapturingSink()
        self.dispatcher.register_sink(LogClass.SYSTEM, self.system_sink)
        self.dispatcher.register_sink(LogClass.STRATEGY, self.strategy_sink)

    def test_dispatch_routes_to_correct_sink(self) -> None:
        self.dispatcher.dispatch(_system_record())
        self.dispatcher.dispatch(_strategy_record())
        self.assertEqual(len(self.system_sink.records), 1)
        self.assertEqual(len(self.strategy_sink.records), 1)
        self.assertIs(self.system_sink.records[0].log_class, LogClass.SYSTEM)
        self.assertIs(self.strategy_sink.records[0].log_class, LogClass.STRATEGY)

    def test_dispatch_rejects_non_record(self) -> None:
        for bad in (None, {"timestamp_ns": 0}, "record", 42):
            with self.assertRaises(LogPayloadError):
                self.dispatcher.dispatch(bad)  # type: ignore[arg-type]

    def test_dispatch_rejects_bad_timestamp(self) -> None:
        for bad in (-1, True, False, 3.14, "1000", None, float("inf"), float("nan")):
            record = _system_record()
            object.__setattr__(record, "timestamp_ns", bad)
            with self.assertRaises(LogPayloadError):
                self.dispatcher.dispatch(record)

    def test_dispatch_rejects_empty_string_fields(self) -> None:
        for field in ("event_type", "message", "correlation_id"):
            for bad in ("", "   ", "\t\n"):
                record = _system_record()
                object.__setattr__(record, field, bad)
                with self.assertRaises(LogPayloadError):
                    self.dispatcher.dispatch(record)

    def test_dispatch_rejects_system_record_with_strategy_id(self) -> None:
        record = _system_record(strategy_id="leaked")
        with self.assertRaises(LogClassError):
            self.dispatcher.dispatch(record)

    def test_dispatch_rejects_strategy_record_without_strategy_id(self) -> None:
        for bad_id in (None, "", "   "):
            record = _strategy_record(strategy_id=bad_id)
            with self.assertRaises(LogClassError):
                self.dispatcher.dispatch(record)

    def test_dispatch_rejects_system_record_with_strategy_source(self) -> None:
        record = _system_record(source=Source.STRATEGY)
        with self.assertRaises(LogClassError):
            self.dispatcher.dispatch(record)

    def test_dispatch_rejects_strategy_record_with_system_source(self) -> None:
        record = _strategy_record(source=Source.KILL_SWITCH, event_type="ACTIVATION")
        with self.assertRaises(LogClassError):
            self.dispatcher.dispatch(record)

    def test_dispatch_rejects_system_event_type_not_in_allowlist(self) -> None:
        record = _system_record(event_type="BOGUS")
        with self.assertRaises(LogPayloadError):
            self.dispatcher.dispatch(record)

    def test_dispatch_accepts_user_defined_event_type_for_strategy(self) -> None:
        record = _strategy_record(event_type="anything_user_picks_here")
        self.dispatcher.dispatch(record)
        self.assertEqual(len(self.strategy_sink.records), 1)

    def test_dispatch_routing_error_without_registered_sink(self) -> None:
        empty = RoutedLogDispatcher()
        with self.assertRaises(LogRoutingError):
            empty.dispatch(_system_record())

    def test_dispatch_wraps_sink_exceptions(self) -> None:
        class _Raiser:
            def write(self, record: LogRecord) -> None:
                raise ValueError("sink-internal failure")

        local = RoutedLogDispatcher()
        local.register_sink(LogClass.SYSTEM, _Raiser())
        with self.assertRaises(LogSinkError) as ctx:
            local.dispatch(_system_record())
        self.assertIsInstance(ctx.exception.__cause__, ValueError)

    def test_register_sink_validates_inputs(self) -> None:
        with self.assertRaises(LogPayloadError):
            self.dispatcher.register_sink("system", _CapturingSink())  # type: ignore[arg-type]

        class _NotASink:
            pass

        with self.assertRaises(LogPayloadError):
            self.dispatcher.register_sink(LogClass.SYSTEM, _NotASink())  # type: ignore[arg-type]

    def test_log_record_is_frozen(self) -> None:
        record = _system_record()
        # frozen dataclasses raise FrozenInstanceError (a dataclass-specific
        # subclass of AttributeError). We match the concrete class so the
        # B017 lint doesn't flag a bare Exception.
        from dataclasses import FrozenInstanceError

        with self.assertRaises(FrozenInstanceError):
            record.message = "mutated"  # type: ignore[misc]

    def test_log_record_as_dict_json_roundtrip(self) -> None:
        record = _system_record()
        payload = record.as_dict()
        # Severity / Source / LogClass must be string values, not enum instances.
        self.assertEqual(payload["severity"], "INFO")
        self.assertEqual(payload["log_class"], "system")
        self.assertEqual(payload["source"], "kill_switch")
        json.dumps(payload)


# --------------------------------------------------------------------------- #
# Mutation rig
# --------------------------------------------------------------------------- #


class _MutationRig:
    """Copy the repo into a tempdir, apply a mutation, and re-run the check."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="atp-logging-mutation-"))
        for entry in ("architecture", "python", "tools"):
            shutil.copytree(ROOT / entry, self.tmp / entry)
        self.env = {
            "PYTHONPATH": str(self.tmp / "python"),
        }

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def replace_in(self, rel: str, old: str, new: str) -> None:
        path = self.tmp / rel
        text = path.read_text(encoding="utf-8")
        if old not in text:
            raise AssertionError(f"mutation source {old!r} not found in {rel}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def patch_contract(self, mutate: Callable[[dict], None]) -> None:
        path = self.tmp / "architecture" / "runtime_services.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        mutate(raw["log_record_contract"])
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    def run_check(self) -> subprocess.CompletedProcess[str]:
        import os

        return subprocess.run(
            [sys.executable, str(self.tmp / "tools" / "log_record_check.py")],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            env={**self.env, "PATH": os.environ.get("PATH", "")},
            check=False,
        )


class LogRecordMutationTest(unittest.TestCase):
    """One L3 negative-case anchor per rule in the contract block."""

    def _run_mutation(
        self, apply: Callable[[_MutationRig], None]
    ) -> subprocess.CompletedProcess[str]:
        rig = _MutationRig()
        try:
            apply(rig)
            return rig.run_check()
        finally:
            rig.cleanup()

    def _assert_fail(self, result: subprocess.CompletedProcess[str], needle: str) -> None:
        self.assertNotEqual(result.returncode, 0, msg=f"expected FAIL; got OK\n{result.stdout}")
        haystack = result.stderr + result.stdout
        self.assertIn(needle, haystack, msg=f"expected {needle!r} in output:\n{haystack}")

    # ---- exports / hierarchy ---- #

    def test_mutation_drops_required_export(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/__init__.py",
                '    "LogRecord",\n',
                "",
            )

        self._assert_fail(self._run_mutation(mutate), "required_exports")

    def test_mutation_breaks_error_hierarchy(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/errors.py",
                "class LogPayloadError(LogRecordError):",
                "class LogPayloadError(Exception):",
            )

        self._assert_fail(self._run_mutation(mutate), "subclass LogRecordError")

    # ---- enum variants ---- #

    def test_mutation_renames_severity_value(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/records.py",
                'CRITICAL = "CRITICAL"',
                'CRITICAL = "CRIT"',
            )

        self._assert_fail(self._run_mutation(mutate), "severity_variants")

    def test_mutation_renames_log_class_value(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/records.py",
                'STRATEGY = "strategy"\n\n\nclass Source',
                'STRATEGY = "strat"\n\n\nclass Source',
            )

        self._assert_fail(self._run_mutation(mutate), "log_class_variants")

    def test_mutation_drops_source_variant(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/records.py",
                '    HOT_SWAP = "hot_swap"\n',
                "",
            )
            # SYSTEM_SOURCES still references HOT_SWAP, which won't load.
            rig.replace_in(
                "python/atp_logging/records.py",
                "        Source.HOT_SWAP,\n",
                "",
            )
            # EVENT_TYPES_BY_SOURCE still references HOT_SWAP.
            rig.replace_in(
                "python/atp_logging/records.py",
                '    Source.HOT_SWAP: ("PROMOTION", "DEMOTION"),\n',
                "",
            )

        self._assert_fail(self._run_mutation(mutate), "source_variants")

    # ---- system/strategy partition ---- #

    def test_mutation_breaks_system_strategy_partition(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            # Move STRATEGY into SYSTEM_SOURCES so the partition overlaps.
            rig.replace_in(
                "python/atp_logging/records.py",
                "STRATEGY_SOURCES: frozenset[Source] = frozenset({Source.STRATEGY})",
                "STRATEGY_SOURCES: frozenset[Source] = frozenset({Source.STRATEGY, Source.KILL_SWITCH})",
            )

        self._assert_fail(self._run_mutation(mutate), "strategy_source_variants")

    # ---- event_types_by_source ---- #

    def test_mutation_drops_event_type_for_source(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/records.py",
                '    Source.KILL_SWITCH: ("ACTIVATION", "HALTED", "LIQUIDATION_TIMEOUT"),\n',
                "    Source.KILL_SWITCH: (),\n",
            )

        self._assert_fail(
            self._run_mutation(mutate),
            "event_types_by_source",
        )

    def test_mutation_adds_event_type_for_strategy(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/records.py",
                "    Source.STRATEGY: (),\n",
                '    Source.STRATEGY: ("USER_DEFINED_FIXED",),\n',
            )

        self._assert_fail(
            self._run_mutation(mutate),
            "event_types_by_source",
        )

    # ---- LogRecord field set / freezing ---- #

    def test_mutation_drops_strategy_id_field(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/records.py",
                "    strategy_id: str | None = field(default=None)\n",
                "",
            )

        self._assert_fail(self._run_mutation(mutate), "required_log_record_fields")

    def test_mutation_unfreezes_log_record(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/records.py",
                "@dataclass(frozen=True, slots=True)",
                "@dataclass(slots=True)",
            )

        self._assert_fail(self._run_mutation(mutate), "frozen")

    # ---- dispatcher validation ---- #

    def test_mutation_drops_timestamp_guard(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/dispatcher.py",
                "if not is_finite_non_negative_int(timestamp_ns):",
                "if False:",
            )

        self._assert_fail(self._run_mutation(mutate), "timestamp_ns")

    def test_mutation_drops_empty_string_guard(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/dispatcher.py",
                "if not value.strip():",
                "if False:",
            )

        self._assert_fail(self._run_mutation(mutate), "empty")

    def test_mutation_drops_enum_discriminant_check(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/dispatcher.py",
                "if not isinstance(record.severity, Severity):",
                "if False:",
            )

        self._assert_fail(self._run_mutation(mutate), "discriminant validation is missing")

    def test_mutation_drops_strategy_id_check_on_system(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/dispatcher.py",
                "if record.strategy_id is not None:",
                "if False:",
            )

        self._assert_fail(self._run_mutation(mutate), "strategy_id")

    def test_mutation_drops_event_type_constraint(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/dispatcher.py",
                "if record.event_type not in allowed_event_types:",
                "if False:",
            )

        self._assert_fail(self._run_mutation(mutate), "event_type")

    # ---- routing ---- #

    def test_mutation_routes_strategy_to_system_sink(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            # Force dispatch to always use the SYSTEM sink, ignoring log_class.
            rig.replace_in(
                "python/atp_logging/dispatcher.py",
                "sink = self._sinks.get(record.log_class)",
                "sink = self._sinks.get(LogClass.SYSTEM)",
            )

        self._assert_fail(self._run_mutation(mutate), "system sink received 2 records")

    def test_mutation_swallows_sink_exception(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            # Drop the except-clause that wraps sink failures.
            rig.replace_in(
                "python/atp_logging/dispatcher.py",
                'raise LogSinkError(\n                f"LogSink for log_class={record.log_class.value!r} raised "\n                f"{type(exc).__name__}: {exc}"\n            ) from exc',
                "pass",
            )

        self._assert_fail(self._run_mutation(mutate), "LogSinkError")

    # ---- dependency direction ---- #

    def test_mutation_imports_upstream_atp_strategy(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/dispatcher.py",
                "from .errors import (",
                "from atp_strategy.api import Strategy  # noqa\nfrom .errors import (",
            )

        self._assert_fail(self._run_mutation(mutate), "upstream")

    # ---- contract deferred list ---- #

    def test_mutation_empties_deferred_list(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            def patch(block: dict) -> None:
                block["deferred"] = []

            rig.patch_contract(patch)

        self._assert_fail(self._run_mutation(mutate), "deferred")

    def test_mutation_drops_srs_log_runtime_from_deferred(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            def patch(block: dict) -> None:
                block["deferred"] = [
                    entry
                    for entry in block["deferred"]
                    if entry["feature"] != "SRS-LOG-001-runtime"
                ]

            rig.patch_contract(patch)

        self._assert_fail(
            self._run_mutation(mutate),
            "SRS-LOG-001-runtime",
        )

    # ---- vendor token isolation ---- #

    def test_mutation_leaks_vendor_token(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_logging/__init__.py",
                '"""Log record SDK-surface for SRS-LOG-001.',
                '"""Log record SDK-surface for SRS-LOG-001 (interactive_brokers comment).',
            )

        self._assert_fail(self._run_mutation(mutate), "vendor")


# --------------------------------------------------------------------------- #
# Pure-Python contract-parity tests (no subprocess)
# --------------------------------------------------------------------------- #


class LogRecordContractBlockParityTest(unittest.TestCase):
    """Cross-check the contract block against the imported package."""

    @classmethod
    def setUpClass(cls) -> None:
        path = ROOT / "architecture" / "runtime_services.json"
        cls.block = json.loads(path.read_text(encoding="utf-8"))["log_record_contract"]

    def test_severity_variants_match_enum(self) -> None:
        self.assertEqual(
            [m.value for m in Severity],
            list(self.block["severity_variants"]),
        )

    def test_log_class_variants_match_enum(self) -> None:
        self.assertEqual(
            [m.value for m in LogClass],
            list(self.block["log_class_variants"]),
        )

    def test_source_variants_match_enum(self) -> None:
        self.assertEqual(
            [m.value for m in Source],
            list(self.block["source_variants"]),
        )

    def test_system_strategy_partition_is_exhaustive(self) -> None:
        system = {s.value for s in SYSTEM_SOURCES}
        strategy = {s.value for s in STRATEGY_SOURCES}
        self.assertEqual(system | strategy, {s.value for s in Source})
        self.assertEqual(system & strategy, set())

    def test_event_types_by_source_matches_contract(self) -> None:
        actual = {src.value: list(types) for src, types in EVENT_TYPES_BY_SOURCE.items()}
        self.assertEqual(actual, self.block["event_types_by_source"])

    def test_module_paths_exist(self) -> None:
        for rel in self.block["module_paths"]:
            self.assertTrue((ROOT / rel).exists(), f"missing module path {rel}")
        self.assertTrue((ROOT / self.block["readme_path"]).exists())

    def test_required_exports_match_dunder_all(self) -> None:
        import atp_logging

        self.assertEqual(sorted(atp_logging.__all__), sorted(self.block["required_exports"]))

    def test_deferred_names_required_downstreams(self) -> None:
        named = {entry["feature"] for entry in self.block["deferred"]}
        for required in ("SRS-LOG-001-runtime", "SRS-UI-001", "SRS-API-001", "SRS-NOTIF-001"):
            self.assertIn(required, named)

    def test_predicate_is_finite_non_negative_int_export(self) -> None:
        # The predicate the dispatcher uses must be exported so an external
        # consumer can re-use it (e.g., a strategy author guarding their own
        # timestamps before constructing a LogRecord).
        self.assertIn("is_finite_non_negative_int", self.block["required_exports"])
        self.assertTrue(is_finite_non_negative_int(0))
        self.assertFalse(is_finite_non_negative_int(-1))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
