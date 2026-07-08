"""Encrypted-at-rest credential vault for SRS-SEC-001 (NFR-S1 / NFR-S4).

Credentials in ATP are supplied through the process environment (``.env`` →
Docker ``env_file`` → ``os.environ``). NFR-S1 / NFR-S4 require that brokerage
and notification credentials are **stored encrypted at rest** and never sit on
disk in plaintext. This module provides the mechanism: a :class:`CredentialVault`
that seals a mapping of secret ``KEY → value`` pairs into a single encrypted
file and opens it back into memory at startup, so an operator can run the stack
without a plaintext ``.env`` on the deployment host.

**Cipher.** Payloads are encrypted with :class:`cryptography.fernet.Fernet`
(AES-128 in CBC mode with a HMAC-SHA256 authentication tag — an authenticated,
misuse-resistant construction; the OWASP-recommended high-level Python
primitive). Decryption is fail-closed: a wrong key or a tampered token raises
:class:`VaultDecryptError` and **never** returns partial plaintext.

**Key material.** Two modes, exactly one per vault:

* *key file* — a gitignored ``*.key`` file (``ATP_VAULT_KEY_FILE``) holding a
  raw urlsafe-base64 Fernet key. The file must be owner-only (``0600``); a
  group/other-readable key file fails closed.
* *passphrase* — ``ATP_VAULT_PASSPHRASE`` run through ``scrypt`` (a random
  16-byte salt is generated at seal time and stored, non-secret, in the
  envelope header).

**On-disk format.** A small JSON envelope whose only ciphertext-bearing field
is the opaque Fernet ``token``; the header (version / kdf / salt) carries no
secret. Written with the project's durable pattern (scratch file → ``fsync`` →
atomic ``os.replace`` → parent-dir ``fsync``; unique scratch; ``0600``; a
missing parent directory fails closed) — mirroring ``atp_safety.state`` and
``JsonlLogStore``.

Rotation *without service restart* is explicitly deferred (SRS §"future
phase"); :meth:`CredentialVault.seal` re-sealing the file (optionally under a
new key) is the supported at-rest re-key path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class VaultError(Exception):
    """Base class for every credential-vault failure."""


class VaultKeyError(VaultError):
    """The vault key material is missing, malformed, or insecurely stored."""


class VaultFormatError(VaultError):
    """The on-disk vault envelope is missing, unreadable, or malformed."""


class VaultDecryptError(VaultError):
    """Decryption failed — wrong key or a tampered/corrupt token.

    Raised in place of returning any plaintext, so a wrong key or a modified
    ciphertext can never leak a partial or attacker-influenced secret.
    """


# --------------------------------------------------------------------------- #
# Constants — envelope schema + scrypt parameters
# --------------------------------------------------------------------------- #

_ENVELOPE_VERSION = 1
_KDF_RAW = "raw"
_KDF_SCRYPT = "scrypt"

# scrypt work factors. n*r*128 bytes of memory (~16 MiB here) — comfortably
# below hashlib's default 32 MiB ``maxmem`` cap while remaining costly to brute
# force, which is proportionate for a single-user local deployment (StRS C-3).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_SALT_BYTES = 16


# --------------------------------------------------------------------------- #
# Key derivation
# --------------------------------------------------------------------------- #


def generate_key() -> str:
    """Return a fresh urlsafe-base64 Fernet key suitable for a key file."""

    return Fernet.generate_key().decode("ascii")


def _fernet_from_raw_key(raw_key: str | bytes) -> Fernet:
    key_bytes = raw_key.encode("ascii") if isinstance(raw_key, str) else raw_key
    try:
        return Fernet(key_bytes)
    except (ValueError, TypeError) as error:
        raise VaultKeyError(
            "vault key is not a valid urlsafe-base64 32-byte Fernet key "
            f"({type(error).__name__}: {error})"
        ) from error


def _fernet_from_passphrase(passphrase: str, salt: bytes) -> Fernet:
    if not passphrase:
        raise VaultKeyError("vault passphrase is empty")
    derived = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return Fernet(base64.urlsafe_b64encode(derived))


def load_key_file(path: str | os.PathLike[str]) -> str:
    """Read and validate a Fernet key from ``path``.

    The key file must exist and be owner-only: a group- or other-readable key
    file fails closed (:class:`VaultKeyError`) — an encrypted vault is
    pointless if its key is world-readable.
    """

    key_path = Path(path)
    try:
        mode = key_path.stat().st_mode
    except FileNotFoundError as error:
        raise VaultKeyError(f"vault key file does not exist: {key_path}") from error
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise VaultKeyError(
            f"vault key file {key_path} is group/other-accessible "
            f"(mode {stat.S_IMODE(mode):#o}); it must be owner-only (0600)"
        )
    raw = key_path.read_text(encoding="ascii").strip()
    if not raw:
        raise VaultKeyError(f"vault key file {key_path} is empty")
    # Validate the material eagerly so a malformed key fails at load, not at
    # first decrypt.
    _fernet_from_raw_key(raw)
    return raw


# --------------------------------------------------------------------------- #
# The vault
# --------------------------------------------------------------------------- #


class CredentialVault:
    """Seal/open an encrypted credential file (SRS-SEC-001).

    Exactly one key mechanism must be supplied: ``key`` (a raw urlsafe-base64
    Fernet key) or ``passphrase`` (scrypt-derived). Both being set, or neither,
    is a :class:`VaultKeyError`.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        key: str | bytes | None = None,
        passphrase: str | None = None,
    ) -> None:
        if (key is None) == (passphrase is None):
            raise VaultKeyError("CredentialVault requires exactly one of key= or passphrase=")
        self._path = Path(path)
        self._key = key
        self._passphrase = passphrase

    @property
    def path(self) -> Path:
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.is_file()

    # -- seal ------------------------------------------------------------- #

    def seal(self, secrets: Mapping[str, str]) -> Path:
        """Encrypt ``secrets`` and durably write the vault file (mode 0600).

        The parent directory must already exist — a missing directory is a
        misconfiguration and fails closed rather than being created somewhere
        unintended.
        """

        payload = _validate_secrets(secrets)
        plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

        if self._passphrase is not None:
            salt = os.urandom(_SALT_BYTES)
            fernet = _fernet_from_passphrase(self._passphrase, salt)
            envelope: dict[str, Any] = {
                "version": _ENVELOPE_VERSION,
                "kdf": _KDF_SCRYPT,
                "scrypt": {"n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P, "dklen": _SCRYPT_DKLEN},
                "salt": base64.b64encode(salt).decode("ascii"),
                "token": fernet.encrypt(plaintext).decode("ascii"),
            }
        else:
            assert self._key is not None  # guaranteed by __init__
            fernet = _fernet_from_raw_key(self._key)
            envelope = {
                "version": _ENVELOPE_VERSION,
                "kdf": _KDF_RAW,
                "token": fernet.encrypt(plaintext).decode("ascii"),
            }

        encoded = json.dumps(envelope, sort_keys=True).encode("utf-8")
        _atomic_write(self._path, encoded)
        return self._path

    # -- open ------------------------------------------------------------- #

    def open(self) -> dict[str, str]:
        """Decrypt the vault file and return the secret mapping in memory.

        Fail-closed: a missing/malformed envelope raises
        :class:`VaultFormatError`; a wrong key or tampered token raises
        :class:`VaultDecryptError` (never a partial plaintext).
        """

        envelope = self._read_envelope()
        token = envelope.get("token")
        if not isinstance(token, str) or not token:
            raise VaultFormatError(f"vault {self._path} envelope is missing a string 'token'")

        kdf = envelope.get("kdf")
        if kdf == _KDF_RAW:
            if self._key is None:
                raise VaultKeyError(
                    f"vault {self._path} was sealed with a raw key; open it with key=, not passphrase="
                )
            fernet = _fernet_from_raw_key(self._key)
        elif kdf == _KDF_SCRYPT:
            if self._passphrase is None:
                raise VaultKeyError(
                    f"vault {self._path} was sealed with a passphrase; "
                    "open it with passphrase=, not key="
                )
            fernet = _fernet_from_passphrase(self._passphrase, _decode_salt(envelope, self._path))
        else:
            raise VaultFormatError(f"vault {self._path} has unknown kdf {kdf!r}")

        try:
            plaintext = fernet.decrypt(token.encode("ascii"))
        except InvalidToken as error:
            raise VaultDecryptError(
                f"vault {self._path} failed to decrypt — wrong key or tampered token"
            ) from error

        try:
            decoded = json.loads(plaintext)
        except json.JSONDecodeError as error:  # pragma: no cover - authenticated payload
            raise VaultDecryptError(
                f"vault {self._path} decrypted to invalid JSON: {error}"
            ) from error
        if not isinstance(decoded, dict):
            raise VaultDecryptError(f"vault {self._path} payload is not a JSON object")
        return {str(k): str(v) for k, v in decoded.items()}

    def _read_envelope(self) -> dict[str, Any]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError as error:
            raise VaultFormatError(f"vault file does not exist: {self._path}") from error
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as error:
            raise VaultFormatError(f"vault {self._path} is not valid JSON: {error}") from error
        if not isinstance(envelope, dict):
            raise VaultFormatError(f"vault {self._path} envelope must be a JSON object")
        return envelope


# --------------------------------------------------------------------------- #
# Startup loader — opt-in overlay of vault secrets onto the process env
# --------------------------------------------------------------------------- #

VAULT_FILE_ENV = "ATP_VAULT_FILE"
VAULT_KEY_FILE_ENV = "ATP_VAULT_KEY_FILE"
VAULT_PASSPHRASE_ENV = "ATP_VAULT_PASSPHRASE"


def load_vault_into_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return ``env`` with vault-sealed secrets overlaid, if a vault is configured.

    **Opt-in and additive.** When ``ATP_VAULT_FILE`` is unset, a copy of ``env``
    is returned unchanged — default behaviour (plaintext ``.env`` / process env)
    is preserved. When it is set, the vault is opened (key from
    ``ATP_VAULT_KEY_FILE`` or passphrase from ``ATP_VAULT_PASSPHRASE``) and its
    decrypted secrets are overlaid onto a copy of ``env`` (vault values win), so
    the operator can run the stack **without** those secrets in a plaintext
    ``.env``. The returned mapping is what ``load_and_validate`` should consume.

    Fail-closed: a configured-but-broken vault (missing file, bad key, tampered
    token) raises a :class:`VaultError` rather than silently falling back to
    plaintext. The vault may contain **only catalogued secret keys** — a vault
    carrying a non-secret config key (e.g. ``ATP_ENV``) is rejected fail-closed,
    so a mis-sealed vault can never overlay and weaken non-secret runtime
    configuration (such as flipping the deployment mode).
    """

    merged = dict(env)
    vault_file = env.get(VAULT_FILE_ENV)
    if not vault_file:
        return merged

    key_file = env.get(VAULT_KEY_FILE_ENV)
    passphrase = env.get(VAULT_PASSPHRASE_ENV)
    if bool(key_file) == bool(passphrase):
        raise VaultKeyError(
            f"{VAULT_FILE_ENV} is set but exactly one of {VAULT_KEY_FILE_ENV} or "
            f"{VAULT_PASSPHRASE_ENV} must also be set"
        )

    if key_file:
        vault = CredentialVault(vault_file, key=load_key_file(key_file))
    else:
        vault = CredentialVault(vault_file, passphrase=passphrase)

    allowed = _catalogued_secret_key_names()
    opened = vault.open()
    unexpected = sorted(set(opened) - allowed)
    if unexpected:
        raise VaultError(
            f"vault {vault_file} contains non-secret key(s) {unexpected}; it may seal only "
            f"catalogued secret keys {sorted(allowed)} — refusing to overlay non-secret config"
        )
    for name, value in opened.items():
        merged[name] = value
    return merged


def _catalogued_secret_key_names() -> frozenset[str]:
    """The catalogue's ``secret=True`` key names — the only keys a vault may hold.

    Imported lazily so ``CredentialVault`` (the crypto primitive) stays usable
    without loading the SRS-ARCH-005 catalogue; only the config-aware loader
    needs the whitelist.
    """

    from .schema import REQUIRED_KEYS

    return frozenset(spec.name for spec in REQUIRED_KEYS if spec.secret)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _validate_secrets(secrets: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(secrets, Mapping):
        raise VaultError(f"secrets must be a mapping; got {type(secrets).__name__}")
    out: dict[str, str] = {}
    for name, value in secrets.items():
        if not isinstance(name, str) or not name:
            raise VaultError("secret names must be non-empty strings")
        if not isinstance(value, str):
            raise VaultError(f"secret {name!r} value must be a string; got {type(value).__name__}")
        out[name] = value
    return out


def _decode_salt(envelope: Mapping[str, Any], path: Path) -> bytes:
    salt_b64 = envelope.get("salt")
    if not isinstance(salt_b64, str) or not salt_b64:
        raise VaultFormatError(f"vault {path} scrypt envelope is missing a 'salt'")
    try:
        return base64.b64decode(salt_b64, validate=True)
    except (ValueError, TypeError) as error:
        raise VaultFormatError(f"vault {path} has a malformed salt: {error}") from error


def _atomic_write(final_path: Path, data: bytes) -> None:
    """Durably write ``data`` to ``final_path`` (mode 0600).

    scratch (``O_EXCL``, unique per pid) → ``fsync`` → atomic ``os.replace`` →
    parent-directory ``fsync``. The parent directory must already exist.
    """

    directory = final_path.parent
    if not directory.is_dir():
        raise VaultError(f"vault directory does not exist: {directory}")
    scratch_path = final_path.with_name(f".{final_path.name}.tmp.{os.getpid()}")
    fd = os.open(scratch_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch_path, final_path)
    except BaseException:
        scratch_path.unlink(missing_ok=True)
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    except OSError:  # pragma: no cover - not all platforms fsync directories
        pass
    finally:
        os.close(dir_fd)


# --------------------------------------------------------------------------- #
# Operator CLI — `python -m atp_config.vault ...` (never prints secret values)
# --------------------------------------------------------------------------- #


def _resolve_cli_vault(args: Any) -> CredentialVault:
    key_file = args.key_file or os.environ.get(VAULT_KEY_FILE_ENV)
    passphrase = os.environ.get(VAULT_PASSPHRASE_ENV)
    if bool(key_file) == bool(passphrase):
        raise VaultKeyError(
            f"provide exactly one of --key-file/{VAULT_KEY_FILE_ENV} or {VAULT_PASSPHRASE_ENV}"
        )
    if key_file:
        return CredentialVault(args.vault_file, key=load_key_file(key_file))
    return CredentialVault(args.vault_file, passphrase=passphrase)


def main(argv: list[str] | None = None) -> int:
    """Minimal operator CLI. Prints key *names* only — never secret values."""

    import argparse

    parser = argparse.ArgumentParser(prog="python -m atp_config.vault")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate-key", help="print a fresh Fernet key (save to a 0600 key file)")

    p_seal = sub.add_parser(
        "seal", help="seal the catalogued secret keys' current env values into the vault"
    )
    p_seal.add_argument("vault_file")
    p_seal.add_argument("--key-file", default=None)

    p_status = sub.add_parser("status", help="open the vault and list sealed key names")
    p_status.add_argument("vault_file")
    p_status.add_argument("--key-file", default=None)

    args = parser.parse_args(argv)

    if args.command == "generate-key":
        print(generate_key())
        return 0

    try:
        if args.command == "seal":
            from .schema import REQUIRED_KEYS

            secrets = {
                spec.name: os.environ[spec.name]
                for spec in REQUIRED_KEYS
                if spec.secret and spec.name in os.environ
            }
            if not secrets:
                print("no catalogued secret keys present in the environment; nothing to seal")
                return 1
            vault = _resolve_cli_vault(args)
            vault.seal(secrets)
            print(f"sealed {len(secrets)} secret(s) into {vault.path}: {sorted(secrets)}")
            return 0

        if args.command == "status":
            vault = _resolve_cli_vault(args)
            names = sorted(vault.open().keys())
            print(f"vault {vault.path} holds {len(names)} sealed secret(s): {names}")
            return 0
    except VaultError as error:
        print(f"vault error: {error}")
        return 1

    return 2  # pragma: no cover - argparse guarantees a known subcommand


__all__ = [
    "CredentialVault",
    "VAULT_FILE_ENV",
    "VAULT_KEY_FILE_ENV",
    "VAULT_PASSPHRASE_ENV",
    "VaultDecryptError",
    "VaultError",
    "VaultFormatError",
    "VaultKeyError",
    "generate_key",
    "load_key_file",
    "load_vault_into_env",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
