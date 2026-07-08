"""Unit tests for the SRS-SEC-001 credential vault (encryption at rest).

Exercises the :class:`atp_config.vault.CredentialVault` round-trip, its
ciphertext-at-rest guarantee, fail-closed decryption, key-file hygiene, and the
opt-in ``load_vault_into_env`` startup loader.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_config.vault import (  # noqa: E402
    VAULT_FILE_ENV,
    VAULT_KEY_FILE_ENV,
    VAULT_PASSPHRASE_ENV,
    CredentialVault,
    VaultDecryptError,
    VaultError,
    VaultFormatError,
    VaultKeyError,
    generate_key,
    load_key_file,
    load_vault_into_env,
)

SECRETS = {
    "ATP_IB_ACCOUNT": "U1234567",
    "ATP_SMTP_API_KEY": "smtp-live-key-abc123DEF456",
    "ATP_SMS_API_KEY": "sms-gw-token-zzz999",
}


class VaultRoundTripTest(unittest.TestCase):
    def test_seal_open_round_trip_key_mode(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            key = generate_key()
            CredentialVault(path, key=key).seal(SECRETS)
            self.assertEqual(CredentialVault(path, key=key).open(), SECRETS)

    def test_seal_open_round_trip_passphrase_mode(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            phrase = "correct horse battery staple"
            CredentialVault(path, passphrase=phrase).seal(SECRETS)
            self.assertEqual(CredentialVault(path, passphrase=phrase).open(), SECRETS)

    def test_reseal_overwrites(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            key = generate_key()
            v = CredentialVault(path, key=key)
            v.seal(SECRETS)
            v.seal({"ATP_SMS_API_KEY": "rotated-value"})
            self.assertEqual(
                CredentialVault(path, key=key).open(), {"ATP_SMS_API_KEY": "rotated-value"}
            )


class CiphertextAtRestTest(unittest.TestCase):
    def test_no_plaintext_secret_in_vault_file(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            CredentialVault(path, key=generate_key()).seal(SECRETS)
            blob = path.read_bytes()
            for value in SECRETS.values():
                self.assertNotIn(value.encode("utf-8"), blob)
            # The header carries no secret; the token is the only ciphertext.
            envelope = json.loads(blob)
            self.assertEqual(envelope["kdf"], "raw")
            self.assertIn("token", envelope)

    def test_vault_file_is_owner_only(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            CredentialVault(path, key=generate_key()).seal(SECRETS)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_seal_leaves_no_scratch_file(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            CredentialVault(path, key=generate_key()).seal(SECRETS)
            leftovers = [p.name for p in Path(d).iterdir() if p.name != "secrets.vault"]
            self.assertEqual(leftovers, [])


class FailClosedTest(unittest.TestCase):
    def test_wrong_key_fails_closed(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            CredentialVault(path, key=generate_key()).seal(SECRETS)
            with self.assertRaises(VaultDecryptError):
                CredentialVault(path, key=generate_key()).open()

    def test_wrong_passphrase_fails_closed(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            CredentialVault(path, passphrase="right").seal(SECRETS)
            with self.assertRaises(VaultDecryptError):
                CredentialVault(path, passphrase="wrong").open()

    def test_tampered_token_fails_closed(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            key = generate_key()
            CredentialVault(path, key=key).seal(SECRETS)
            envelope = json.loads(path.read_bytes())
            token = envelope["token"]
            # Corrupt an interior ciphertext character (a trailing char could be
            # base64-malleable — its low bits truncated by padding — and decode
            # to identical bytes; a mid-token char reliably changes the payload).
            i = len(token) // 2
            flipped = token[:i] + ("A" if token[i] != "A" else "B") + token[i + 1 :]
            envelope["token"] = flipped
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaises(VaultError):  # InvalidToken -> VaultDecryptError
                CredentialVault(path, key=key).open()

    def test_missing_file_fails_closed(self) -> None:
        with TemporaryDirectory() as d:
            with self.assertRaises(VaultFormatError):
                CredentialVault(Path(d) / "absent.vault", key=generate_key()).open()

    def test_missing_directory_fails_closed(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "nope" / "secrets.vault"
            with self.assertRaises(VaultError):
                CredentialVault(path, key=generate_key()).seal(SECRETS)

    def test_kdf_mismatch_fails_closed(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            key = generate_key()
            CredentialVault(path, key=key).seal(SECRETS)
            # Sealed with a raw key; opening with a passphrase must refuse.
            with self.assertRaises(VaultKeyError):
                CredentialVault(path, passphrase="x").open()


class KeyMaterialTest(unittest.TestCase):
    def test_requires_exactly_one_key_mechanism(self) -> None:
        with self.assertRaises(VaultKeyError):
            CredentialVault("x.vault")  # neither
        with self.assertRaises(VaultKeyError):
            CredentialVault("x.vault", key=generate_key(), passphrase="p")  # both

    def test_malformed_key_rejected(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            with self.assertRaises(VaultKeyError):
                CredentialVault(path, key="not-a-fernet-key").seal(SECRETS)

    def test_load_key_file_rejects_group_readable(self) -> None:
        with TemporaryDirectory() as d:
            kf = Path(d) / "vault.key"
            kf.write_text(generate_key())
            os.chmod(kf, 0o640)  # group-readable
            with self.assertRaises(VaultKeyError):
                load_key_file(kf)

    def test_load_key_file_accepts_owner_only(self) -> None:
        with TemporaryDirectory() as d:
            kf = Path(d) / "vault.key"
            key = generate_key()
            kf.write_text(key)
            os.chmod(kf, 0o600)
            self.assertEqual(load_key_file(kf), key)

    def test_load_key_file_missing(self) -> None:
        with TemporaryDirectory() as d:
            with self.assertRaises(VaultKeyError):
                load_key_file(Path(d) / "absent.key")


class SecretValidationTest(unittest.TestCase):
    def test_non_string_value_rejected(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            with self.assertRaises(VaultError):
                CredentialVault(path, key=generate_key()).seal({"K": 123})  # type: ignore[dict-item]

    def test_empty_name_rejected(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            with self.assertRaises(VaultError):
                CredentialVault(path, key=generate_key()).seal({"": "v"})


class LoadVaultIntoEnvTest(unittest.TestCase):
    def test_opt_in_no_vault_returns_copy(self) -> None:
        env = {"A": "1", "B": "2"}
        merged = load_vault_into_env(env)
        self.assertEqual(merged, env)
        self.assertIsNot(merged, env)

    def test_overlay_from_key_file(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            key = generate_key()
            CredentialVault(path, key=key).seal(SECRETS)
            kf = Path(d) / "vault.key"
            kf.write_text(key)
            os.chmod(kf, 0o600)
            env = {
                VAULT_FILE_ENV: str(path),
                VAULT_KEY_FILE_ENV: str(kf),
                "ATP_IB_ACCOUNT": "placeholder-set-in-environment",
                "OTHER": "keep",
            }
            merged = load_vault_into_env(env)
            self.assertEqual(merged["ATP_IB_ACCOUNT"], "U1234567")  # vault overrides
            self.assertEqual(merged["ATP_SMTP_API_KEY"], SECRETS["ATP_SMTP_API_KEY"])
            self.assertEqual(merged["OTHER"], "keep")

    def test_overlay_from_passphrase(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            CredentialVault(path, passphrase="pp").seal({"ATP_SMS_API_KEY": "sealed"})
            env = {VAULT_FILE_ENV: str(path), VAULT_PASSPHRASE_ENV: "pp"}
            self.assertEqual(load_vault_into_env(env)["ATP_SMS_API_KEY"], "sealed")

    def test_ambiguous_key_config_fails_closed(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            env = {
                VAULT_FILE_ENV: str(path),
                VAULT_KEY_FILE_ENV: "k",
                VAULT_PASSPHRASE_ENV: "p",
            }
            with self.assertRaises(VaultKeyError):
                load_vault_into_env(env)

    def test_non_secret_key_in_vault_rejected(self) -> None:
        # A vault may seal ONLY catalogued secret keys. A non-secret key (e.g.
        # ATP_ENV) must fail closed so a mis-sealed vault cannot overlay and
        # weaken non-secret runtime config (like the deployment mode).
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            key = generate_key()
            CredentialVault(path, key=key).seal({"ATP_ENV": "development"})
            kf = Path(d) / "vault.key"
            kf.write_text(key)
            os.chmod(kf, 0o600)
            env = {VAULT_FILE_ENV: str(path), VAULT_KEY_FILE_ENV: str(kf)}
            with self.assertRaises(VaultError):
                load_vault_into_env(env)

    def test_broken_vault_fails_closed_not_silent(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "secrets.vault"
            CredentialVault(path, key=generate_key()).seal(SECRETS)
            kf = Path(d) / "vault.key"
            kf.write_text(generate_key())  # WRONG key
            os.chmod(kf, 0o600)
            env = {VAULT_FILE_ENV: str(path), VAULT_KEY_FILE_ENV: str(kf)}
            with self.assertRaises(VaultDecryptError):
                load_vault_into_env(env)


if __name__ == "__main__":
    unittest.main()
