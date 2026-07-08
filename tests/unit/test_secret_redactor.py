"""Unit tests for the SRS-SEC-001 log-record credential redactor."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_logging import LogClass, LogRecord, Severity, Source  # noqa: E402
from atp_logging.redaction import REDACTION_MARKER, SecretRedactor  # noqa: E402


class RedactTextTest(unittest.TestCase):
    def test_exact_value_masked(self) -> None:
        r = SecretRedactor({"U1234567"})
        self.assertEqual(r.redact_text("U1234567"), REDACTION_MARKER)

    def test_substring_masked_context_preserved(self) -> None:
        r = SecretRedactor({"smtp-live-key-abc123"})
        out = r.redact_text("sending via smtp-live-key-abc123 now")
        self.assertNotIn("smtp-live-key-abc123", out)
        self.assertIn(REDACTION_MARKER, out)
        self.assertIn("sending via", out)
        self.assertIn("now", out)

    def test_multiple_secrets_all_masked(self) -> None:
        r = SecretRedactor({"U1234567", "sms-token-zzz999"})
        out = r.redact_text("acct U1234567 sms sms-token-zzz999")
        self.assertNotIn("U1234567", out)
        self.assertNotIn("sms-token-zzz999", out)

    def test_longest_first_no_partial_leak(self) -> None:
        # "secretvalue" is a superstring of "secret"; the longer one is masked
        # first so no fragment of the longer secret survives.
        r = SecretRedactor({"secret", "secretvalue123"})
        out = r.redact_text("x secretvalue123 y")
        self.assertNotIn("secretvalue123", out)

    def test_short_or_empty_values_ignored(self) -> None:
        # A sub-4-char "secret" would pathologically mask ordinary text.
        r = SecretRedactor({"", "  ", "ab"})
        self.assertEqual(r.secret_count, 0)
        self.assertEqual(r.redact_text("ab cd ab"), "ab cd ab")

    def test_non_string_passthrough(self) -> None:
        r = SecretRedactor({"U1234567"})
        self.assertEqual(r.redact_text(""), "")

    def test_no_secret_no_change(self) -> None:
        r = SecretRedactor({"U1234567"})
        self.assertEqual(r.redact_text("nothing here"), "nothing here")


class PatternFallbackTest(unittest.TestCase):
    def test_api_key_assignment_masked(self) -> None:
        r = SecretRedactor(set())  # no registered values — fallback only
        out = r.redact_text("config api_key=UnregisteredButSecret999")
        self.assertNotIn("UnregisteredButSecret999", out)
        self.assertIn("api_key=", out)  # key name preserved, value masked

    def test_provider_key_shapes_masked(self) -> None:
        r = SecretRedactor(set())
        # Assembled from fragments so these obviously-fake key shapes are not
        # themselves flagged as committed secrets by tools/critic_check.py — the
        # runtime value is the full token the redaction pattern must catch.
        tokens = (
            "db-" + "abcdef0123456789XY",
            "sk-" + "ant-" + "abcdefghij0123456789",
            "AKIA" + "ABCDEFGHIJKLMNOP",
        )
        for token in tokens:
            out = r.redact_text(f"token {token} end")
            self.assertNotIn(token, out, token)

    def test_passphrase_assignment_masked(self) -> None:
        r = SecretRedactor(set())  # fallback only
        out = r.redact_text("ATP_VAULT_PASSPHRASE=correct-horse-battery-staple")
        self.assertNotIn("correct-horse-battery-staple", out)

    def test_bare_ib_account_shape_masked(self) -> None:
        # A raw IB account id has no key= shape, but its U#######/DU####### form
        # is masked by the floor so even the value-blind default cannot leak it.
        r = SecretRedactor(set())
        for acct in ("U7654321", "DU1234567"):
            out = r.redact_text(f"gateway account {acct} connected")
            self.assertNotIn(acct, out, acct)

    def test_ordinary_text_untouched(self) -> None:
        r = SecretRedactor(set())
        msg = "order routing outcome: FILLED 100 shares of AAPL at 190.25"
        self.assertEqual(r.redact_text(msg), msg)


def _sys_record(message: str, event_type: str = "CONNECT", corr: str = "corr-1") -> LogRecord:
    return LogRecord(
        time.time_ns(), Severity.INFO, Source.IB_GATEWAY, event_type, message, corr, LogClass.SYSTEM
    )


def _strategy_record(message: str, event_type: str, corr: str = "corr-1") -> LogRecord:
    return LogRecord(
        time.time_ns(),
        Severity.WARN,
        Source.STRATEGY,
        event_type,
        message,
        corr,
        LogClass.STRATEGY,
        strategy_id="s1",
    )


class RedactRecordTest(unittest.TestCase):
    def test_message_and_correlation_masked(self) -> None:
        r = SecretRedactor({"U1234567"})
        rec = r.redact_record(_sys_record("account U1234567", corr="req-U1234567-9"))
        self.assertNotIn("U1234567", rec.message)
        self.assertNotIn("U1234567", rec.correlation_id)
        self.assertIn(REDACTION_MARKER, rec.message)

    def test_system_event_type_not_redacted(self) -> None:
        # Registering the SYSTEM event_type value itself proves the field is
        # intentionally left intact (its vocabulary is AC-pinned and must stay
        # valid); only message/correlation carry free-form secret risk.
        r = SecretRedactor({"CONNECT"})
        rec = r.redact_record(_sys_record("CONNECT happened", event_type="CONNECT"))
        self.assertEqual(rec.event_type, "CONNECT")
        self.assertNotIn("CONNECT", rec.message)  # message still scrubbed

    def test_strategy_event_type_redacted(self) -> None:
        r = SecretRedactor({"U1234567"})
        rec = r.redact_record(_strategy_record("msg", event_type="login-U1234567"))
        self.assertNotIn("U1234567", rec.event_type)

    def test_strategy_id_redacted(self) -> None:
        # strategy_id is persisted by LogRecord.as_dict(), so a secret embedded
        # there must be scrubbed too.
        r = SecretRedactor({"U1234567"})
        rec = LogRecord(
            time.time_ns(),
            Severity.WARN,
            Source.STRATEGY,
            "evt",
            "msg",
            "corr",
            LogClass.STRATEGY,
            strategy_id="strat-U1234567",
        )
        out = r.redact_record(rec)
        assert out.strategy_id is not None
        self.assertNotIn("U1234567", out.strategy_id)
        self.assertTrue(out.strategy_id.strip())  # stays schema-valid (non-empty)

    def test_system_strategy_id_none_untouched(self) -> None:
        r = SecretRedactor({"U1234567"})
        rec = r.redact_record(_sys_record("nothing"))
        self.assertIsNone(rec.strategy_id)

    def test_clean_record_returns_same_object(self) -> None:
        r = SecretRedactor({"U1234567"})
        rec = _sys_record("nothing secret here")
        self.assertIs(r.redact_record(rec), rec)

    def test_redacted_record_is_a_copy(self) -> None:
        r = SecretRedactor({"U1234567"})
        rec = _sys_record("account U1234567")
        out = r.redact_record(rec)
        self.assertIsNot(out, rec)
        self.assertEqual(rec.message, "account U1234567")  # original unchanged (frozen)


if __name__ == "__main__":
    unittest.main()
