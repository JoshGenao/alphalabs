from __future__ import annotations

import inspect
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_config  # noqa: E402
from atp_config import (  # noqa: E402
    CATEGORIES,
    PLACEHOLDER_VALUE,
    REQUIRED_KEYS,
    Category,
    KeyType,
    ReadinessFailure,
    ReadinessReport,
    Severity,
    load_and_validate,
)


def _defaults() -> dict[str, str]:
    return {spec.name: spec.default for spec in REQUIRED_KEYS if spec.default is not None}


class CatalogueShapeTest(unittest.TestCase):
    def test_every_category_has_at_least_one_key(self) -> None:
        declared = {spec.category for spec in REQUIRED_KEYS}
        for category in CATEGORIES:
            self.assertIn(
                category,
                declared,
                f"category {category.value!r} has no catalogued key",
            )

    def test_every_key_has_srs_trace(self) -> None:
        for spec in REQUIRED_KEYS:
            self.assertTrue(
                spec.srs_trace,
                f"{spec.name} has no SRS trace; SRS-ARCH-005 requires traceability",
            )

    def test_every_key_has_default(self) -> None:
        for spec in REQUIRED_KEYS:
            self.assertIsNotNone(
                spec.default,
                f"{spec.name} has no default; init.sh dev mode would fail without one",
            )

    def test_secret_keys_carry_secret_flag(self) -> None:
        for spec in REQUIRED_KEYS:
            if spec.type is KeyType.SECRET:
                self.assertTrue(
                    spec.secret,
                    f"{spec.name} is type SECRET but secret flag is False",
                )

    def test_required_traceability_to_srs_arch_005_categories(self) -> None:
        # The SRS row enumerates six categories; the catalogue must cover them.
        expected = {
            Category.CREDENTIALS,
            Category.STORAGE_PATHS,
            Category.IB_ACCOUNT,
            Category.MARKET_DATA_LIMITS,
            Category.RESOURCE_LIMITS,
            Category.NOTIFICATION_CHANNELS,
        }
        self.assertEqual(set(CATEGORIES), expected)


class ValidatorBehaviourTest(unittest.TestCase):
    def test_default_env_passes_in_development(self) -> None:
        report = load_and_validate(_defaults())
        self.assertTrue(report.ok, [f.as_dict() for f in report.errors])
        self.assertEqual(len(report.errors), 0)
        # Placeholder secrets surface as warnings.
        self.assertEqual(len(report.warnings), 4)

    def test_missing_required_key_fails_with_structured_failure(self) -> None:
        env = _defaults()
        env.pop("DATABENTO_API_KEY")
        report = load_and_validate(env)
        self.assertFalse(report.ok)
        databento = next(f for f in report.errors if f.key == "DATABENTO_API_KEY")
        self.assertEqual(databento.category, Category.CREDENTIALS)
        self.assertEqual(databento.severity, Severity.ERROR)
        self.assertIn("not set", databento.reason)
        self.assertIn("SRS-DATA-001", databento.srs_trace)

    def test_placeholder_secret_escalates_in_production(self) -> None:
        env = _defaults()
        env["ATP_ENV"] = "production"
        report = load_and_validate(env)
        self.assertFalse(report.ok)
        secret_errors = [f for f in report.errors if f.key.endswith("_API_KEY")]
        self.assertEqual(
            {f.key for f in secret_errors},
            {"ATP_SMTP_API_KEY", "ATP_SMS_API_KEY", "DATABENTO_API_KEY", "SHARADAR_API_KEY"},
        )
        for failure in secret_errors:
            self.assertEqual(failure.severity, Severity.ERROR)
            self.assertIn("production", failure.reason)

    def test_placeholder_secret_warns_in_development(self) -> None:
        report = load_and_validate(_defaults())
        secret_warnings = [f for f in report.warnings if f.key.endswith("_API_KEY")]
        self.assertEqual(len(secret_warnings), 4)
        for warning in secret_warnings:
            self.assertEqual(warning.severity, Severity.WARNING)

    def test_invalid_int_detected(self) -> None:
        env = _defaults()
        env["ATP_MARKET_DATA_LINE_LIMIT"] = "oops"
        report = load_and_validate(env)
        self.assertFalse(report.ok)
        failure = next(f for f in report.errors if f.key == "ATP_MARKET_DATA_LINE_LIMIT")
        self.assertIn("expected integer", failure.reason)

    def test_int_range_check(self) -> None:
        env = _defaults()
        env["ATP_MARKET_DATA_LINE_LIMIT"] = "0"
        report = load_and_validate(env)
        self.assertFalse(report.ok)
        failure = next(f for f in report.errors if f.key == "ATP_MARKET_DATA_LINE_LIMIT")
        self.assertIn("below min", failure.reason)

    def test_invalid_float_detected(self) -> None:
        env = _defaults()
        env["ATP_LIVE_STRATEGY_CPU"] = "not-a-float"
        report = load_and_validate(env)
        self.assertFalse(report.ok)
        failure = next(f for f in report.errors if f.key == "ATP_LIVE_STRATEGY_CPU")
        self.assertIn("expected float", failure.reason)

    def test_invalid_path_detected(self) -> None:
        env = _defaults()
        env["ATP_SSD_DATA_DIR"] = "relative/path"
        report = load_and_validate(env)
        self.assertFalse(report.ok)
        failure = next(f for f in report.errors if f.key == "ATP_SSD_DATA_DIR")
        self.assertIn("absolute", failure.reason)

    def test_empty_path_detected(self) -> None:
        env = _defaults()
        env["ATP_SSD_DATA_DIR"] = ""
        report = load_and_validate(env)
        self.assertFalse(report.ok)
        failure = next(f for f in report.errors if f.key == "ATP_SSD_DATA_DIR")
        self.assertIn("empty", failure.reason)

    def test_invalid_enum_detected(self) -> None:
        env = _defaults()
        env["ATP_ENV"] = "qa"  # not in {development, staging, production}
        report = load_and_validate(env)
        self.assertFalse(report.ok)
        failure = next(f for f in report.errors if f.key == "ATP_ENV")
        self.assertIn("allowed choices", failure.reason)


class ReadinessFailureShapeTest(unittest.TestCase):
    def test_failure_carries_required_fields(self) -> None:
        report = load_and_validate({})
        self.assertGreater(len(report.failures), 0)
        for failure in report.failures:
            self.assertIsInstance(failure, ReadinessFailure)
            self.assertTrue(failure.key)
            self.assertIsInstance(failure.category, Category)
            self.assertIsInstance(failure.severity, Severity)
            self.assertTrue(failure.reason)
            self.assertIsInstance(failure.srs_trace, tuple)

    def test_as_dict_has_documented_keys(self) -> None:
        report = load_and_validate({})
        first = report.failures[0]
        data = first.as_dict()
        self.assertEqual(
            set(data.keys()),
            {"key", "category", "severity", "reason", "srs_trace"},
        )

    def test_as_json_line_round_trips(self) -> None:
        report = load_and_validate({})
        for failure in report.failures:
            line = failure.as_json_line()
            parsed = json.loads(line)
            self.assertEqual(parsed["key"], failure.key)
            self.assertEqual(parsed["category"], failure.category.value)
            self.assertEqual(parsed["severity"], failure.severity.value)


class ReadinessReportTest(unittest.TestCase):
    def test_ok_is_false_when_any_error_present(self) -> None:
        report = load_and_validate({})
        self.assertFalse(report.ok)

    def test_warnings_do_not_flip_ok_to_false(self) -> None:
        report = load_and_validate(_defaults())
        self.assertTrue(report.ok)
        self.assertGreater(len(report.warnings), 0)

    def test_evidence_lines_cover_every_category(self) -> None:
        report = load_and_validate(_defaults())
        joined = "\n".join(report.evidence)
        for category in Category:
            self.assertIn(category.value, joined)


class PublicSurfaceTest(unittest.TestCase):
    def test_public_objects_have_docstrings(self) -> None:
        for name in atp_config.__all__:
            obj = getattr(atp_config, name)
            if inspect.isclass(obj) or inspect.isfunction(obj):
                self.assertTrue(
                    obj.__doc__,
                    f"atp_config.{name} is missing a docstring (SRS-ARCH-005 'documented')",
                )

    def test_placeholder_value_constant_matches_env_example(self) -> None:
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn(PLACEHOLDER_VALUE, env_example)

    def test_required_keys_count_matches_runtime_services(self) -> None:
        catalogue = json.loads(
            (ROOT / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(catalogue["configuration"]["keys"]), len(REQUIRED_KEYS))


if __name__ == "__main__":
    unittest.main()
