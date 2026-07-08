"""Credential redaction for the SRS-LOG-001 log path (SRS-SEC-001 / NFR-S1,S4).

NFR-S1 / NFR-S4 require that brokerage (IB) and notification (SMTP, SMS)
credentials are **never emitted in plaintext to logs**. The structured-log
subsystem (:mod:`atp_logging`) is the single place persisted log bytes are
produced, so this module scrubs secrets there.

:class:`SecretRedactor` is **value-based**: it is built from the actual live
secret values (the caller sources these from the configuration catalogue's
``secret`` keys — e.g. ``SecretRedactor(atp_config.secret_values(env))``) and
replaces any occurrence of those values in a
:class:`~atp_logging.LogRecord`'s free-form fields with a constant marker. This
is the strong guarantee — if the operator's real IB account / SMTP key / SMS
key appears anywhere in a message, it is masked. A conservative **pattern
fallback** additionally masks secret-shaped tokens (``api_key=…``, provider key
shapes) so a secret that was *not* registered still cannot leak.

The redactor is threaded into :class:`~atp_logging.JsonlLogStore` (the
persistence choke point — so a record written directly to a store, bypassing
the dispatcher, is still redacted) and :class:`~atp_logging.RoutedLogDispatcher`.
Redaction operates by **substring replacement**, so only the secret portion of
a value is masked and the surrounding text (e.g. the non-secret part of a
correlation id) is preserved for traceability.

This module stays free of any ``atp_config`` import — ``atp_logging`` is the
foundational SDK that every consumer (including ``atp_config``) may depend on,
never the reverse (enforced by ``tools/log_record_check.py``). The secret
*values* are therefore injected by the caller, not read here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace

from .records import LogClass, LogRecord

REDACTION_MARKER = "***REDACTED***"
"""Masked-value placeholder. Matches ``ConfigHandler.REDACTION_MARKER`` so the
config view (``atp_runtime``) and the log path present secrets identically."""

_MIN_SECRET_LEN = 4
"""Registered secret values shorter than this are ignored for *value*-based
redaction — a 1–3 character 'secret' would pathologically mask ordinary log
text. The pattern fallback still applies. Real IB/SMTP/SMS credentials are far
longer, so this never weakens the guarantee for genuine secrets."""

# Conservative secret-shaped patterns (mirrors tools/critic_check.py's
# SECRET_PATTERNS). Group 1, where present, is the value to mask.
#
# The ``passphrase`` alternative covers the SRS-SEC-001 vault-unlock secret
# (``ATP_VAULT_PASSPHRASE``); the trailing ``D?U\d{7}`` rule masks a bare
# Interactive Brokers account id (live ``U#######`` / paper ``DU#######``) so
# even the value-blind DEFAULT_REDACTOR floor cannot persist a raw IB account.
_PATTERN_RULES: tuple[tuple[re.Pattern[str], int], ...] = (
    (
        # The optional ``[a-z0-9_]*`` prefix lets an env-var-style identifier
        # match too (e.g. ``ATP_VAULT_PASSPHRASE=…`` / ``MY_API_KEY=…``), where
        # the leading underscore would otherwise block a bare ``\b`` boundary.
        re.compile(
            r"(?i)\b[a-z0-9_]*(?:api[_-]?key|secret|token|password|passphrase|passwd|pwd)\b"
            r"\s*[=:]\s*(\S+)"
        ),
        1,
    ),
    (re.compile(r"\bdb-[A-Za-z0-9]{16,}\b"), 0),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}\b"), 0),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    (re.compile(r"\bD?U\d{7}\b"), 0),
)


class SecretRedactor:
    """Scrub registered secret values (and secret-shaped tokens) from log records.

    ``secret_values`` are the live plaintext credentials to mask. Empty,
    whitespace-only, and sub-``_MIN_SECRET_LEN`` values are dropped. Matching is
    longest-first so an overlapping/substring secret cannot leave a longer
    secret partially exposed.
    """

    def __init__(self, secret_values: Iterable[str], *, marker: str = REDACTION_MARKER) -> None:
        cleaned = {
            value
            for value in secret_values
            if isinstance(value, str) and len(value.strip()) >= _MIN_SECRET_LEN
        }
        # Longest-first: mask the most specific (longest) secret before any
        # shorter secret that might be a substring of it.
        self._values: tuple[str, ...] = tuple(sorted(cleaned, key=len, reverse=True))
        self._marker = marker

    @property
    def secret_count(self) -> int:
        """Number of registered secret values (post-filtering)."""

        return len(self._values)

    def redact_text(self, text: str) -> str:
        """Return ``text`` with every registered secret value + secret-shaped
        token replaced by the marker."""

        if not isinstance(text, str) or not text:
            return text
        redacted = text
        for value in self._values:
            if value in redacted:
                redacted = redacted.replace(value, self._marker)
        for pattern, group in _PATTERN_RULES:
            redacted = self._apply_pattern(pattern, group, redacted)
        return redacted

    def _apply_pattern(self, pattern: re.Pattern[str], group: int, text: str) -> str:
        marker = self._marker

        def _sub(match: re.Match[str]) -> str:
            if group == 0:
                return marker
            # Replace only the captured value, preserving the key= prefix.
            whole = match.group(0)
            value = match.group(group)
            return whole.replace(value, marker, 1)

        return pattern.sub(_sub, text)

    def redact_record(self, record: LogRecord) -> LogRecord:
        """Return a redacted copy of ``record`` (or ``record`` if nothing matched).

        Every free-form field that ``LogRecord.as_dict()`` persists is scrubbed:
        ``message`` and ``correlation_id`` always; ``event_type`` and
        ``strategy_id`` on ``STRATEGY`` records (both free-form / user-supplied,
        so both can carry a credential). A SYSTEM record's ``event_type`` is a
        fixed AC-pinned vocabulary that must stay valid and never holds a secret,
        and its ``strategy_id`` is always ``None``, so neither is touched.
        Redacting a STRATEGY ``strategy_id`` to the (non-empty) marker keeps the
        record schema-valid.
        """

        new_message = self.redact_text(record.message)
        new_correlation = self.redact_text(record.correlation_id)
        if record.log_class is LogClass.STRATEGY:
            new_event_type = self.redact_text(record.event_type)
            new_strategy_id = (
                self.redact_text(record.strategy_id)
                if isinstance(record.strategy_id, str)
                else record.strategy_id
            )
        else:
            new_event_type = record.event_type
            new_strategy_id = record.strategy_id

        if (
            new_message == record.message
            and new_correlation == record.correlation_id
            and new_event_type == record.event_type
            and new_strategy_id == record.strategy_id
        ):
            return record
        return replace(
            record,
            message=new_message,
            correlation_id=new_correlation,
            event_type=new_event_type,
            strategy_id=new_strategy_id,
        )


DEFAULT_REDACTOR = SecretRedactor(())
"""The always-on *pattern-based* redactor used when no value-aware redactor is
injected. It registers no secret values, but its secret-shaped-token fallback
still runs — ``api_key=…`` / ``passphrase=…`` assignments, provider key shapes,
and a bare Interactive Brokers account id (``U#######`` / ``DU#######``) — so
the default log path is never *zero* redaction, and even a raw IB account can't
slip through. Production boot injects the value-aware redactor —
``SecretRedactor(atp_config.secret_values(env))`` (see
``atp_logging_boot.build_boot_log_dispatcher``) — for full IB/SMTP/SMS coverage
including arbitrarily-shaped SMTP/SMS keys; this constant is the safe floor when
it does not."""


__all__ = [
    "DEFAULT_REDACTOR",
    "REDACTION_MARKER",
    "SecretRedactor",
]
