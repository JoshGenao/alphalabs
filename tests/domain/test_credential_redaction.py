"""L7 domain test: credential encryption-at-rest + log redaction (``SRS-SEC-001``).

SRS-SEC-001 / NFR-S1 / NFR-S4 / StRS C-3, SN-1.12 — the trading-system safety
invariant that brokerage (IB) and notification (SMTP, SMS) credentials are
**encrypted at rest** and **never emitted in plaintext to logs**. Both halves of
the acceptance criteria are proved here end to end:

  1. *Encryption at rest.* A :class:`~atp_config.CredentialVault` seals the IB /
     SMTP / SMS secrets to a file; the file bytes contain no plaintext secret and
     round-trip back only under the correct key.

  2. *Log redaction.* A redactor built from the catalogue's ``secret`` keys is
     installed on the SRS-LOG-001 boot dispatcher + both persistent stores.
     Records embedding each secret — via the dispatcher AND written directly to a
     store (the bypass path) — are read back from disk and none of the IB / SMTP /
     SMS plaintext appears; the redaction marker does.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_config import REQUIRED_KEYS, secret_values  # noqa: E402
from atp_config.vault import CredentialVault, generate_key  # noqa: E402
from atp_logging import LogClass, LogRecord, Severity, Source  # noqa: E402
from atp_logging.persistence import build_separated_log_dispatcher  # noqa: E402
from atp_logging.redaction import REDACTION_MARKER, SecretRedactor  # noqa: E402
from atp_logging_boot import build_boot_log_dispatcher  # noqa: E402
from atp_readiness import GateState, ReadinessGate  # noqa: E402

pytestmark = [pytest.mark.domain, pytest.mark.safety]

# Representative real-shaped credentials for the three AC-named channels.
IB_ACCOUNT = "U7654321"
SMTP_KEY = "smtp-live-key-9f8e7d6c5b4a3210"
SMS_KEY = "sms-gw-token-abcdef0123456789"

# The env an operator would supply (placeholders for the two vendor keys prove
# placeholders are NOT treated as live secrets).
ENV = {
    "ATP_IB_ACCOUNT": IB_ACCOUNT,
    "ATP_SMTP_API_KEY": SMTP_KEY,
    "ATP_SMS_API_KEY": SMS_KEY,
    "DATABENTO_API_KEY": "placeholder-set-in-environment",
    "SHARADAR_API_KEY": "placeholder-set-in-environment",
}
LIVE_SECRETS = (IB_ACCOUNT, SMTP_KEY, SMS_KEY)


def test_catalogue_secret_values_cover_ib_smtp_sms() -> None:
    values = secret_values(ENV)
    assert IB_ACCOUNT in values, "IB account (ATP_IB_ACCOUNT) must be a catalogue secret"
    assert SMTP_KEY in values
    assert SMS_KEY in values
    # Placeholders are not live secrets.
    assert "placeholder-set-in-environment" not in values


def test_credentials_encrypted_at_rest() -> None:
    with TemporaryDirectory() as d:
        vault_path = Path(d) / "secrets.vault"
        key = generate_key()
        CredentialVault(vault_path, key=key).seal(
            {"ATP_IB_ACCOUNT": IB_ACCOUNT, "ATP_SMTP_API_KEY": SMTP_KEY, "ATP_SMS_API_KEY": SMS_KEY}
        )
        blob = vault_path.read_bytes()
        for secret in LIVE_SECRETS:
            assert secret.encode("utf-8") not in blob, f"plaintext {secret!r} in vault file"
        # Round-trips only under the correct key.
        assert CredentialVault(vault_path, key=key).open() == {
            "ATP_IB_ACCOUNT": IB_ACCOUNT,
            "ATP_SMTP_API_KEY": SMTP_KEY,
            "ATP_SMS_API_KEY": SMS_KEY,
        }


def test_default_store_construction_is_never_zero_redaction() -> None:
    # Codex R2: a store/dispatcher built with NO injected redactor must still
    # redact — the default is the pattern-based floor, never plaintext. A
    # secret-shaped token written through the default path is scrubbed on disk.
    shaped_secret = "api_key=" + "Zx9UnregisteredButLeaked01234"
    with TemporaryDirectory() as d:
        dispatcher, system_store, strategy_store = build_separated_log_dispatcher(d)  # no redactor
        try:
            dispatcher.dispatch(
                LogRecord(
                    time.time_ns(),
                    Severity.WARN,
                    Source.STRATEGY,
                    "evt",
                    f"leaking {shaped_secret} oops",
                    "c",
                    LogClass.STRATEGY,
                    strategy_id="s",
                )
            )
        finally:
            system_store.close()
            strategy_store.close()
        persisted = (Path(d) / "strategy.jsonl").read_text("utf-8")
        assert "Zx9UnregisteredButLeaked01234" not in persisted
        assert REDACTION_MARKER in persisted


def test_production_boot_factory_masks_ib_account_value_without_injection() -> None:
    # Codex R3: the sanctioned production boot path must source real secret
    # VALUES from config itself — a bare IB account id (no telltale shape) must
    # be masked WITHOUT the test injecting a redactor. build_boot_log_dispatcher
    # builds SecretRedactor(secret_values(env)) internally.
    with TemporaryDirectory() as d:
        dispatcher, system_store, strategy_store = build_boot_log_dispatcher(d, ENV)
        try:
            dispatcher.dispatch(
                LogRecord(
                    time.time_ns(),
                    Severity.INFO,
                    Source.IB_GATEWAY,
                    "CONNECT",
                    f"gateway up for account {IB_ACCOUNT}",  # bare value, no api_key= shape
                    f"trace-{IB_ACCOUNT}",
                    LogClass.SYSTEM,
                )
            )
        finally:
            system_store.close()
            strategy_store.close()
        persisted = (Path(d) / "system.jsonl").read_text("utf-8")
        assert IB_ACCOUNT not in persisted, "production boot path leaked a bare IB account value"
        assert REDACTION_MARKER in persisted


def test_boot_factory_direct_store_write_masks_arbitrary_smtp_value() -> None:
    # Codex R2/R6: a DIRECT JsonlLogStore.write (bypassing the dispatcher) with an
    # ARBITRARY (unshaped) SMTP value must be scrubbed on the value-aware
    # production path — not only secret-shaped tokens.
    arbitrary_smtp = "9f8e7d6c5b4a"  # no api_key= shape, no provider prefix
    env = {"ATP_SMTP_API_KEY": arbitrary_smtp}
    with TemporaryDirectory() as d:
        _dispatcher, system_store, strategy_store = build_boot_log_dispatcher(d, env)
        try:
            strategy_store.write(
                LogRecord(
                    time.time_ns(),
                    Severity.ERROR,
                    Source.STRATEGY,
                    "direct",
                    f"smtp auth used {arbitrary_smtp}",
                    "c",
                    LogClass.STRATEGY,
                    strategy_id="s",
                )
            )
        finally:
            system_store.close()
            strategy_store.close()
        assert arbitrary_smtp not in (Path(d) / "strategy.jsonl").read_text("utf-8")


def test_boot_factory_reads_secrets_from_the_vault() -> None:
    # The boot factory overlays the encrypted vault first, so a secret that
    # exists ONLY in the vault (not in the plaintext env) is still redacted.
    with TemporaryDirectory() as d:
        vault_path = Path(d) / "secrets.vault"
        key_file = Path(d) / "vault.key"
        key = generate_key()
        key_file.write_text(key)
        key_file.chmod(0o600)
        vault_only_secret = "U0099887"
        CredentialVault(vault_path, key=key).seal({"ATP_IB_ACCOUNT": vault_only_secret})
        env = {
            "ATP_IB_ACCOUNT": "placeholder-set-in-environment",  # NOT the real value
            "ATP_VAULT_FILE": str(vault_path),
            "ATP_VAULT_KEY_FILE": str(key_file),
        }
        dispatcher, system_store, strategy_store = build_boot_log_dispatcher(d, env)
        try:
            dispatcher.dispatch(
                LogRecord(
                    time.time_ns(),
                    Severity.INFO,
                    Source.IB_GATEWAY,
                    "CONNECT",
                    f"account {vault_only_secret}",
                    "trace-x",
                    LogClass.SYSTEM,
                )
            )
        finally:
            system_store.close()
            strategy_store.close()
        assert vault_only_secret not in (Path(d) / "system.jsonl").read_text("utf-8")


def test_boot_factory_redacts_the_vault_passphrase() -> None:
    # Codex R4: ATP_VAULT_PASSPHRASE is a vault-unlock secret; a startup log that
    # echoes it must not persist it in plaintext. The boot factory registers it.
    passphrase = "correct-horse-battery-staple-42"
    with TemporaryDirectory() as d:
        vault_path = Path(d) / "secrets.vault"
        CredentialVault(vault_path, passphrase=passphrase).seal({"ATP_SMS_API_KEY": "sealed-sms"})
        env = {"ATP_VAULT_FILE": str(vault_path), "ATP_VAULT_PASSPHRASE": passphrase}
        dispatcher, system_store, strategy_store = build_boot_log_dispatcher(d, env)
        try:
            dispatcher.dispatch(
                LogRecord(
                    time.time_ns(),
                    Severity.WARN,
                    Source.STRATEGY,
                    "cfg",
                    f"loaded vault with passphrase {passphrase}",
                    "c",
                    LogClass.STRATEGY,
                    strategy_id="s",
                )
            )
        finally:
            system_store.close()
            strategy_store.close()
        assert passphrase not in (Path(d) / "strategy.jsonl").read_text("utf-8")


def test_secrets_never_reach_logs_in_plaintext() -> None:
    redactor = SecretRedactor(secret_values(ENV))
    with TemporaryDirectory() as d:
        dispatcher, system_store, strategy_store = build_separated_log_dispatcher(
            d, redactor=redactor
        )
        try:
            ts = time.time_ns()
            # IB secret via the dispatcher, in a SYSTEM message + correlation id.
            dispatcher.dispatch(
                LogRecord(
                    ts,
                    Severity.INFO,
                    Source.IB_GATEWAY,
                    "CONNECT",
                    f"IB gateway connected for account {IB_ACCOUNT}",
                    f"trace-{IB_ACCOUNT}",
                    LogClass.SYSTEM,
                )
            )
            # SMTP + SMS secrets via the dispatcher, in a STRATEGY record.
            dispatcher.dispatch(
                LogRecord(
                    ts,
                    Severity.WARN,
                    Source.STRATEGY,
                    f"notify-{SMS_KEY}",
                    f"alert via smtp {SMTP_KEY} and sms {SMS_KEY}",
                    "c-strategy",
                    LogClass.STRATEGY,
                    strategy_id="s1",
                )
            )
            # Bypass path: write DIRECTLY to the store (not through the dispatcher)
            # to prove redaction is enforced at the persistence boundary too.
            strategy_store.write(
                LogRecord(
                    ts,
                    Severity.ERROR,
                    Source.STRATEGY,
                    "direct-write",
                    f"leaked key {SMTP_KEY} written straight to the store",
                    f"corr-{IB_ACCOUNT}",
                    LogClass.STRATEGY,
                    strategy_id=f"strat-{SMS_KEY}",  # a secret in strategy_id must scrub too
                )
            )
        finally:
            system_store.close()
            strategy_store.close()

        persisted = (Path(d) / "system.jsonl").read_text("utf-8") + (
            Path(d) / "strategy.jsonl"
        ).read_text("utf-8")

        for secret in LIVE_SECRETS:
            assert secret not in persisted, f"{secret!r} leaked into the persisted logs"
        assert REDACTION_MARKER in persisted

        # Every persisted line is still valid JSON with the required fields —
        # redaction preserved the schema.
        for line in persisted.splitlines():
            record = json.loads(line)
            assert record["message"] and record["correlation_id"]


def test_compose_isolated_services_blank_all_vault_material() -> None:
    # Codex R10: the x-atp-no-secrets anchor (merged over *atp-env by jupyter /
    # IB Gateway / strategy-runtime) must blank EVERY catalogued secret AND all
    # vault-unlock material — including ATP_VAULT_PASSPHRASE — so a passphrase-
    # mode deployment cannot leak vault key material into an isolated service.
    compose = (ROOT / "docker-compose.yml").read_text("utf-8")
    anchor = compose.split("x-atp-no-secrets:", 1)[1].split("\nservices:", 1)[0]
    for key in (
        "ATP_IB_ACCOUNT",
        "ATP_SMTP_API_KEY",
        "ATP_SMS_API_KEY",
        "DATABENTO_API_KEY",
        "SHARADAR_API_KEY",
        "ATP_VAULT_FILE",
        "ATP_VAULT_KEY_FILE",
        "ATP_VAULT_PASSPHRASE",
    ):
        assert f'{key}: ""' in anchor, f"{key} not blanked in x-atp-no-secrets"


def _prod_defaults() -> dict[str, str]:
    """Production env with every catalogue key at its default (secrets = placeholder)."""
    env = {spec.name: spec.default for spec in REQUIRED_KEYS if spec.default is not None}
    env["ATP_ENV"] = "production"
    return env


def test_readiness_gate_consumes_the_vault() -> None:
    # In production, placeholder secrets are hard errors -> pre-trade blocked.
    # Sealing the real secrets in a vault and pointing the gate at it must clear
    # those errors WITHOUT any plaintext secret in the environment.
    with TemporaryDirectory() as d:
        vault_path = Path(d) / "secrets.vault"
        key_file = Path(d) / "vault.key"
        key = generate_key()
        key_file.write_text(key)
        key_file.chmod(0o600)
        CredentialVault(vault_path, key=key).seal(
            {spec.name: f"real-{spec.name}" for spec in REQUIRED_KEYS if spec.secret}
        )

        blocked = ReadinessGate.from_env(_prod_defaults())
        assert blocked.state is GateState.PRE_TRADE_BLOCKED  # placeholders in prod

        env = _prod_defaults()  # secrets still placeholders in the ENV itself
        env["ATP_VAULT_FILE"] = str(vault_path)
        env["ATP_VAULT_KEY_FILE"] = str(key_file)
        ready = ReadinessGate.from_env(env)
        assert ready.state is GateState.READY, [f.as_dict() for f in ready.report.errors]


def test_production_plaintext_secret_is_rejected() -> None:
    # Codex R5: encryption at rest must be ENFORCED — a real secret value sitting
    # in the plaintext production environment (no vault) fails readiness, so
    # SRS-SEC-001 cannot be silently bypassed with a plaintext .env.
    env = _prod_defaults()
    env["ATP_IB_ACCOUNT"] = "U7654321"  # a REAL value in plaintext, no vault
    gate = ReadinessGate.from_env(env)
    assert gate.state is GateState.PRE_TRADE_BLOCKED
    offending = {f.key for f in gate.report.errors if "vault" in f.reason.lower()}
    assert "ATP_IB_ACCOUNT" in offending, [f.as_dict() for f in gate.report.errors]


def test_development_plaintext_secret_is_allowed() -> None:
    # Dev keeps plaintext-env flexibility: the vault-at-rest mandate is
    # staging/production only (proportionate to a single-user local context).
    env = {spec.name: spec.default for spec in REQUIRED_KEYS if spec.default is not None}
    env["ATP_ENV"] = "development"
    env["ATP_IB_ACCOUNT"] = "U7654321"  # real plaintext value is fine in dev
    gate = ReadinessGate.from_env(env)
    assert gate.state is GateState.READY, [f.as_dict() for f in gate.report.errors]


def test_vault_cannot_downgrade_deployment_mode() -> None:
    # Codex R10: a vault carrying a NON-secret key (ATP_ENV=development) must not
    # be able to flip production readiness to a laxer mode — the load fails
    # closed, so the production placeholder-secret errors still hold.
    with TemporaryDirectory() as d:
        vault_path = Path(d) / "secrets.vault"
        key_file = Path(d) / "vault.key"
        key = generate_key()
        key_file.write_text(key)
        key_file.chmod(0o600)
        CredentialVault(vault_path, key=key).seal({"ATP_ENV": "development"})
        env = _prod_defaults()
        env["ATP_VAULT_FILE"] = str(vault_path)
        env["ATP_VAULT_KEY_FILE"] = str(key_file)
        gate = ReadinessGate.from_env(env)
        assert gate.state is GateState.PRE_TRADE_BLOCKED


def test_broken_vault_holds_pre_trade_fail_closed() -> None:
    with TemporaryDirectory() as d:
        vault_path = Path(d) / "secrets.vault"
        key_file = Path(d) / "vault.key"
        CredentialVault(vault_path, key=generate_key()).seal({"ATP_SMTP_API_KEY": "x"})
        key_file.write_text(generate_key())  # WRONG key
        key_file.chmod(0o600)

        env = _prod_defaults()
        env["ATP_VAULT_FILE"] = str(vault_path)
        env["ATP_VAULT_KEY_FILE"] = str(key_file)
        gate = ReadinessGate.from_env(env)
        assert gate.state is GateState.PRE_TRADE_BLOCKED
        assert any(f.key == "ATP_VAULT_FILE" for f in gate.report.errors)
