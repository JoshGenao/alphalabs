"""SRS-SEC-001 — property proof that registered secrets never survive redaction.

L2 property layer. The L7 domain test pins the guarantee on fixed IB/SMTP/SMS
values through the real dispatcher + store; this layer generalises it over
Hypothesis-generated secrets and surrounding noise: a registered secret value,
wherever it appears in a log field, is never emitted in plaintext.

Secrets are drawn from an alphanumeric alphabet (min length 8) to model real
IB account ids / SMTP / SMS credentials — this deliberately excludes the
pathological case of a "secret" that is itself a substring of the redaction
marker, which value-based masking cannot help (a real credential is never
``***REDACTED***``).
"""

from __future__ import annotations

import string
import sys
import time
from pathlib import Path

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_logging import LogClass, LogRecord, Severity, Source  # noqa: E402
from atp_logging.redaction import SecretRedactor  # noqa: E402

pytestmark = pytest.mark.property

_SECRET = st.text(alphabet=string.ascii_letters + string.digits, min_size=8, max_size=48)
_NOISE = st.text(max_size=64)


@given(secret=_SECRET, prefix=_NOISE, suffix=_NOISE)
def test_registered_secret_never_survives_text(secret: str, prefix: str, suffix: str) -> None:
    assume(secret not in prefix and secret not in suffix)
    redactor = SecretRedactor({secret})
    out = redactor.redact_text(f"{prefix}{secret}{suffix}")
    assert secret not in out


@given(secret=_SECRET, message=_NOISE, corr=_NOISE)
def test_registered_secret_never_survives_record(secret: str, message: str, corr: str) -> None:
    assume(secret not in message and secret not in corr)
    redactor = SecretRedactor({secret})
    record = LogRecord(
        timestamp_ns=time.time_ns(),
        severity=Severity.WARN,
        source=Source.STRATEGY,
        event_type="user-event",
        message=f"{message} {secret}",
        correlation_id=f"{secret}-{corr}",
        log_class=LogClass.STRATEGY,
        strategy_id="s1",
    )
    out = redactor.redact_record(record)
    assert secret not in out.message
    assert secret not in out.correlation_id


@given(noise=_NOISE)
def test_no_registered_secret_is_noop(noise: str) -> None:
    # With no registered secrets and no secret-shaped tokens, ordinary text is
    # returned unchanged (the pattern fallback must not mangle normal logs).
    assume("=" not in noise and ":" not in noise)
    assume(
        not any(tok in noise.lower() for tok in ("api", "key", "secret", "token", "pass", "pwd"))
    )
    assume("db-" not in noise and "sk-" not in noise and "AKIA" not in noise)
    redactor = SecretRedactor(set())
    assert redactor.redact_text(noise) == noise
