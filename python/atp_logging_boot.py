"""Production composition root for the SRS-LOG-001 log subsystem (SRS-SEC-001).

``atp_logging`` is the foundational logging SDK and must stay free of any
``atp_config`` import (enforced by ``tools/log_record_check.py``), so it can
never source the operator's real secret *values* on its own — its default
factory only pattern-masks token-shaped strings. Value-based redaction of an IB
account / SMTP / SMS credential therefore has to be composed one layer up, by
the code that owns both config and logging.

This module is that layer. :func:`build_boot_log_dispatcher` is the sanctioned
production entry point: it overlays the encrypted credential vault
(``load_vault_into_env``), builds a value-aware
``SecretRedactor(secret_values(resolved_env))``, and wires it into the separated
system/strategy dispatcher — so the *default* production boot path masks real
brokerage/notification credentials without the caller having to inject anything.

Low-level callers that construct ``build_separated_log_dispatcher`` directly
still get the always-on pattern-based floor (``DEFAULT_REDACTOR``) — never zero
redaction — but production boot should go through here for full value coverage.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from atp_config import secret_values
from atp_config.vault import (
    VAULT_KEY_FILE_ENV,
    VAULT_PASSPHRASE_ENV,
    VaultError,
    load_key_file,
    load_vault_into_env,
)
from atp_logging import RoutedLogDispatcher
from atp_logging.persistence import JsonlLogStore, build_separated_log_dispatcher
from atp_logging.redaction import SecretRedactor


def build_boot_log_dispatcher(
    directory: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    *,
    max_bytes: int | None = None,
    max_files: int = 5,
    fsync: bool = True,
    system_filename: str = "system.jsonl",
    strategy_filename: str = "strategy.jsonl",
) -> tuple[RoutedLogDispatcher, JsonlLogStore, JsonlLogStore]:
    """Wire the separated system/strategy log dispatcher with value-aware redaction.

    ``env`` defaults to ``os.environ``. The SRS-SEC-001 credential vault is
    overlaid first (so vault-sealed secrets are also redacted), then a redactor
    is built from the live values of every catalogued ``secret`` key **plus the
    vault-unlock secrets** (``ATP_VAULT_PASSPHRASE`` and the key-file contents),
    and installed on the dispatcher + both stores. A configured-but-broken vault
    raises a :class:`atp_config.vault.VaultError` — the boot fails closed rather
    than logging against unknown credentials.
    """

    source = os.environ if env is None else env
    resolved = load_vault_into_env(source)
    values = secret_values(resolved) | _vault_unlock_secrets(source)
    redactor = SecretRedactor(values)
    return build_separated_log_dispatcher(
        directory,
        max_bytes=max_bytes,
        max_files=max_files,
        fsync=fsync,
        system_filename=system_filename,
        strategy_filename=strategy_filename,
        redactor=redactor,
    )


def _vault_unlock_secrets(env: Mapping[str, str]) -> set[str]:
    """The vault-unlock secrets that must also never appear in a log.

    These are NOT catalogued config keys but they are credentials in their own
    right (they decrypt the vault): the passphrase and the raw key-file contents.
    A broken/missing key file is ignored here — ``load_vault_into_env`` already
    fails the boot closed on it.
    """

    out: set[str] = set()
    passphrase = env.get(VAULT_PASSPHRASE_ENV)
    if passphrase:
        out.add(passphrase)
    key_file = env.get(VAULT_KEY_FILE_ENV)
    if key_file:
        try:
            out.add(load_key_file(key_file))
        except VaultError:
            pass
    return out


__all__ = ["build_boot_log_dispatcher"]
