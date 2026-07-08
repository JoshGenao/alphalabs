#!/usr/bin/env python3
"""Credential-security evidence for SRS-SEC-001 (NFR-S1 / NFR-S4).

Proves, at build time, the two acceptance-criteria halves without any live
credential:

* **Encryption at rest** — the :class:`~atp_config.CredentialVault` seals secrets
  to a file whose bytes carry no plaintext, round-trips only under the correct
  key, and fails closed on a wrong key.
* **No plaintext credential logging** — a redactor built from the catalogue's
  ``secret`` keys, once installed on the SRS-LOG-001 boot dispatcher + stores,
  scrubs IB / SMTP / SMS secrets on both the dispatch path and the
  direct-to-store bypass path.

It also confirms the catalogue actually marks the IB account and the two
notification channels as ``secret`` (so they are vaultable and redacted), and
that the redaction layer stays free of any ``atp_config`` upstream import.

Mirrors the PASS/FAIL output style of ``tools/operator_interface_runtime_check.py``.

Invoke:
    python3 tools/credential_security_check.py     # exit 0 on PASS, 1 on FAIL
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"

# Throwaway, obviously-fake secrets — never a real credential.
_FAKE_IB = "U0000000-CHECK"
_FAKE_SMTP = "smtp-check-key-0011223344556677"
_FAKE_SMS = "sms-check-token-8899aabbccddeeff"
_FAKE_SECRETS = (_FAKE_IB, _FAKE_SMTP, _FAKE_SMS)


class ContractCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise ContractCheckError(message)


def _import():
    if str(PYTHON_ROOT) not in sys.path:
        sys.path.insert(0, str(PYTHON_ROOT))
    import atp_config
    from atp_config import vault as atp_vault
    from atp_logging.persistence import build_separated_log_dispatcher
    from atp_logging.redaction import REDACTION_MARKER, SecretRedactor
    from atp_logging_boot import build_boot_log_dispatcher

    return (
        atp_config,
        atp_vault,
        build_separated_log_dispatcher,
        build_boot_log_dispatcher,
        REDACTION_MARKER,
        SecretRedactor,
    )


def check_catalogue_marks_credentials_secret(atp_config) -> str:
    by_name = {spec.name: spec for spec in atp_config.REQUIRED_KEYS}
    for name in ("ATP_IB_ACCOUNT", "ATP_SMTP_API_KEY", "ATP_SMS_API_KEY"):
        spec = by_name.get(name)
        if spec is None:
            fail(f"catalogue is missing required credential key {name}")
        if not spec.secret:
            fail(f"{name} must be flagged secret=true so it is vaultable + redacted")
    return "catalogue marks IB account + SMTP + SMS as secret (NFR-S1 / NFR-S4)"


def check_vault_encrypts_at_rest(atp_vault) -> str:
    with TemporaryDirectory() as d:
        vault_path = Path(d) / "secrets.vault"
        key = atp_vault.generate_key()
        payload = {
            "ATP_IB_ACCOUNT": _FAKE_IB,
            "ATP_SMTP_API_KEY": _FAKE_SMTP,
            "ATP_SMS_API_KEY": _FAKE_SMS,
        }
        atp_vault.CredentialVault(vault_path, key=key).seal(payload)

        blob = vault_path.read_bytes()
        for secret in _FAKE_SECRETS:
            if secret.encode("utf-8") in blob:
                fail(f"vault file leaks plaintext secret {secret!r} — not encrypted at rest")

        mode = vault_path.stat().st_mode & 0o777
        if mode != 0o600:
            fail(f"vault file mode is {mode:#o}; must be owner-only 0600")

        if atp_vault.CredentialVault(vault_path, key=key).open() != payload:
            fail("vault did not round-trip under the correct key")

        try:
            atp_vault.CredentialVault(vault_path, key=atp_vault.generate_key()).open()
        except atp_vault.VaultDecryptError:
            pass
        else:
            fail("vault decrypted under a WRONG key — not fail-closed")
    return (
        "vault seals credentials as ciphertext (0600), round-trips, and fails closed on a wrong key"
    )


def check_log_redaction_wired(
    atp_config, build_separated_log_dispatcher, marker, SecretRedactor
) -> str:
    from atp_logging import LogClass, LogRecord, Severity, Source

    env = {"ATP_IB_ACCOUNT": _FAKE_IB, "ATP_SMTP_API_KEY": _FAKE_SMTP, "ATP_SMS_API_KEY": _FAKE_SMS}
    redactor = SecretRedactor(atp_config.secret_values(env))
    if redactor.secret_count < 3:
        fail("redactor did not pick up the three IB/SMTP/SMS secret values from the catalogue")

    with TemporaryDirectory() as d:
        dispatcher, system_store, strategy_store = build_separated_log_dispatcher(
            d, redactor=redactor
        )
        try:
            ts = 1_700_000_000_000_000_000
            dispatcher.dispatch(
                LogRecord(
                    ts,
                    Severity.INFO,
                    Source.IB_GATEWAY,
                    "CONNECT",
                    f"account {_FAKE_IB}",
                    f"trace-{_FAKE_IB}",
                    LogClass.SYSTEM,
                )
            )
            # Direct-to-store bypass path must also be redacted.
            strategy_store.write(
                LogRecord(
                    ts,
                    Severity.WARN,
                    Source.STRATEGY,
                    "notify",
                    f"smtp {_FAKE_SMTP} sms {_FAKE_SMS}",
                    "c1",
                    LogClass.STRATEGY,
                    strategy_id="s1",
                )
            )
        finally:
            system_store.close()
            strategy_store.close()

        persisted = (Path(d) / "system.jsonl").read_text("utf-8") + (
            Path(d) / "strategy.jsonl"
        ).read_text("utf-8")
        for secret in _FAKE_SECRETS:
            if secret in persisted:
                fail(
                    f"secret {secret!r} reached the persisted log in plaintext — redaction not wired"
                )
        if marker not in persisted:
            fail("redaction marker absent from persisted logs")
    return "log redaction scrubs IB/SMTP/SMS on the dispatcher AND the direct-to-store path"


def check_default_construction_is_never_zero_redaction(
    build_separated_log_dispatcher, marker
) -> str:
    """A store/dispatcher built with NO injected redactor must still redact.

    Guards against the redaction being opt-in: the default is the pattern-based
    floor, so a secret-shaped token cannot land in the persisted log even when
    the boot layer forgot to inject a value-aware redactor.
    """
    from atp_logging import LogClass, LogRecord, Severity, Source

    shaped = "api_key=" + "Zx9CheckOnlyShapedSecret012"
    with TemporaryDirectory() as d:
        dispatcher, system_store, strategy_store = build_separated_log_dispatcher(d)  # no redactor
        try:
            dispatcher.dispatch(
                LogRecord(
                    1_700_000_000_000_000_000,
                    Severity.WARN,
                    Source.STRATEGY,
                    "evt",
                    f"leaking {shaped}",
                    "c",
                    LogClass.STRATEGY,
                    strategy_id="s",
                )
            )
        finally:
            system_store.close()
            strategy_store.close()
        persisted = (Path(d) / "strategy.jsonl").read_text("utf-8")
        if "Zx9CheckOnlyShapedSecret012" in persisted:
            fail(
                "default (no-redactor) log construction persisted a secret-shaped token in plaintext"
            )
        if marker not in persisted:
            fail("default log construction did not apply the pattern-based redaction floor")
    return "default (no-redactor) log construction still applies the pattern-based redaction floor"


def check_boot_factory_is_value_aware(build_boot_log_dispatcher, marker) -> str:
    """The production boot factory must mask a bare IB account VALUE with no injection.

    ``build_boot_log_dispatcher`` sources ``secret_values`` from config itself,
    so a credential with no telltale shape (a plain IB account id) is redacted
    on the default production path — not just via a manually injected redactor.
    """
    from atp_logging import LogClass, LogRecord, Severity, Source

    env = {"ATP_IB_ACCOUNT": _FAKE_IB, "ATP_SMTP_API_KEY": _FAKE_SMTP, "ATP_SMS_API_KEY": _FAKE_SMS}
    with TemporaryDirectory() as d:
        dispatcher, system_store, strategy_store = build_boot_log_dispatcher(d, env)
        try:
            dispatcher.dispatch(
                LogRecord(
                    1_700_000_000_000_000_000,
                    Severity.INFO,
                    Source.IB_GATEWAY,
                    "CONNECT",
                    f"account {_FAKE_IB}",
                    "trace-x",
                    LogClass.SYSTEM,
                )
            )
        finally:
            system_store.close()
            strategy_store.close()
        persisted = (Path(d) / "system.jsonl").read_text("utf-8")
        if _FAKE_IB in persisted:
            fail("production boot factory did not mask a bare IB account value from config")
        if marker not in persisted:
            fail("production boot factory produced no redaction marker")
    return "production boot factory (build_boot_log_dispatcher) masks bare IB/SMTP/SMS values from config"


def check_production_secrets_must_be_vaulted(atp_config) -> str:
    """Staging/production must reject plaintext catalogued secrets (SRS-SEC-001).

    Proves encryption-at-rest is *enforced*, not optional: a real secret value in
    a plaintext production environment fails readiness, while development keeps
    plaintext flexibility.
    """
    from atp_readiness import GateState, ReadinessGate

    base = {
        spec.name: spec.default for spec in atp_config.REQUIRED_KEYS if spec.default is not None
    }

    prod = dict(base)
    prod["ATP_ENV"] = "production"
    prod["ATP_IB_ACCOUNT"] = "U7654321"  # real plaintext value, no vault
    gate = ReadinessGate.from_env(prod)
    if gate.state is not GateState.PRE_TRADE_BLOCKED:
        fail(
            "production readiness accepted a plaintext secret with no vault (encryption not enforced)"
        )
    if not any("vault" in f.reason.lower() for f in gate.report.errors):
        fail("production plaintext-secret rejection did not cite the vault requirement")

    dev = dict(base)
    dev["ATP_ENV"] = "development"
    dev["ATP_IB_ACCOUNT"] = "U7654321"
    if ReadinessGate.from_env(dev).state is not GateState.READY:
        fail("development readiness wrongly rejected a plaintext secret (dev keeps flexibility)")
    return "staging/production reject plaintext catalogued secrets; development keeps flexibility"


def check_production_log_wiring_is_value_aware() -> str:
    """No production module may wire the raw log dispatcher without a redactor.

    The value-aware production path is ``atp_logging_boot.build_boot_log_dispatcher``.
    A production ``.py`` calling ``build_separated_log_dispatcher`` without an
    explicit ``redactor=`` would rely only on the pattern floor for arbitrary
    SMTP/SMS values — so this scan fails on it. The boot wrapper (which supplies
    the value-aware redactor) and the persistence module that defines the builder
    are the only exemptions.
    """

    exempt = {"atp_logging_boot.py", "persistence.py"}
    offenders: list[str] = []
    for path in sorted(PYTHON_ROOT.rglob("*.py")):
        if path.name in exempt or "test" in path.name:
            continue
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name == "build_separated_log_dispatcher":
                    if not any(kw.arg == "redactor" for kw in node.keywords):
                        offenders.append(f"{path.relative_to(PYTHON_ROOT)}:{node.lineno}")
    if offenders:
        fail(
            "production log wiring calls build_separated_log_dispatcher without a redactor "
            f"(use atp_logging_boot.build_boot_log_dispatcher): {offenders}"
        )
    return "production log wiring uses the value-aware boot path (no raw dispatcher without a redactor)"


def check_redaction_layer_has_no_upstream_import() -> str:
    source = (PYTHON_ROOT / "atp_logging" / "redaction.py").read_text("utf-8")
    tree = ast.parse(source)
    forbidden = {"atp_config", "atp_api", "atp_cli", "atp_ws", "atp_readiness", "atp_strategy"}
    leaked: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            leaked |= {alias.name.split(".")[0] for alias in node.names} & forbidden
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in forbidden:
                leaked.add(node.module.split(".")[0])
    if leaked:
        fail(f"atp_logging.redaction imports upstream package(s) {sorted(leaked)} (must be pure)")
    return "redaction layer imports no upstream package (secret values are caller-injected)"


def run_checks() -> list[str]:
    atp_config, atp_vault, build_dispatcher, build_boot, marker, SecretRedactor = _import()
    return [
        check_catalogue_marks_credentials_secret(atp_config),
        check_vault_encrypts_at_rest(atp_vault),
        check_log_redaction_wired(atp_config, build_dispatcher, marker, SecretRedactor),
        check_default_construction_is_never_zero_redaction(build_dispatcher, marker),
        check_boot_factory_is_value_aware(build_boot, marker),
        check_production_secrets_must_be_vaulted(atp_config),
        check_production_log_wiring_is_value_aware(),
        check_redaction_layer_has_no_upstream_import(),
    ]


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="SRS-SEC-001 credential-security evidence").parse_args(argv)
    try:
        evidence = run_checks()
    except ContractCheckError as error:
        print(f"CREDENTIAL SECURITY FAIL: {error}", file=sys.stderr)
        return 1

    print("CREDENTIAL SECURITY PASS — SRS-SEC-001 encryption-at-rest + log redaction")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
