"""Envelope encryption for secrets at rest.

CLAUDE.md rule 7: exchange API keys + Telegram session strings + any other operator
secret are NEVER stored plaintext. They're encrypted with a master key sourced from
env (eventually KMS), the ciphertext goes into the DB column (`*_enc` bytea), and the
plaintext is reconstituted only at the moment a connector needs it.

We use Fernet (AES-128-CBC + HMAC-SHA256, authenticated) — well-trusted, no nonce
management, tampered ciphertext raises cleanly. The master key is a base64-encoded
32-byte value generated with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Store the result as `CHALYBCRYPTO_MASTER_ENCRYPTION_KEY`. NEVER commit it.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

__all__ = ["SecretsVault", "InvalidToken", "vault_from_env"]


class SecretsVault:
    """Tiny Fernet wrapper. Constructed with the base64 key directly so tests can
    inject a fixed key without touching env."""

    def __init__(self, master_key_b64: str) -> None:
        if not master_key_b64:
            raise ValueError("SecretsVault: master_key_b64 is required")
        self._fernet = Fernet(master_key_b64.encode("ascii"))

    def encrypt(self, plaintext: str) -> bytes:
        if not isinstance(plaintext, str):
            raise TypeError(f"SecretsVault.encrypt: expected str, got {type(plaintext).__name__}")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        if not isinstance(ciphertext, (bytes, bytearray)):
            raise TypeError(
                f"SecretsVault.decrypt: expected bytes, got {type(ciphertext).__name__}"
            )
        return self._fernet.decrypt(bytes(ciphertext)).decode("utf-8")


def vault_from_env(*, env_var: str = "CHALYBCRYPTO_MASTER_ENCRYPTION_KEY") -> SecretsVault:
    """Build a vault from the configured env var. Raises clearly if unset."""
    key = os.environ.get(env_var)
    if not key:
        raise RuntimeError(
            f"{env_var} is unset — generate one with "
            "`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`"
        )
    return SecretsVault(key)
