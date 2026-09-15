"""SecretsVault — envelope encryption for at-rest secrets."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from chalybcrypto_shared import InvalidToken, SecretsVault, vault_from_env


# A fixed test key so the tests are deterministic. NEVER hardcode in prod code.
_TEST_KEY = Fernet.generate_key().decode("ascii")


def test_round_trip_preserves_plaintext():
    v = SecretsVault(_TEST_KEY)
    ct = v.encrypt("hello world")
    assert isinstance(ct, bytes)
    assert v.decrypt(ct) == "hello world"


def test_ciphertext_differs_each_call_even_for_same_plaintext():
    """Fernet uses a fresh IV every call; ciphertexts must NOT match."""
    v = SecretsVault(_TEST_KEY)
    a = v.encrypt("same")
    b = v.encrypt("same")
    assert a != b


def test_tampered_ciphertext_raises_invalid_token():
    v = SecretsVault(_TEST_KEY)
    ct = bytearray(v.encrypt("secret"))
    ct[-5] ^= 0x01  # flip a bit late in the payload
    with pytest.raises(InvalidToken):
        v.decrypt(bytes(ct))


def test_decrypt_with_wrong_key_raises_invalid_token():
    v1 = SecretsVault(_TEST_KEY)
    v2 = SecretsVault(Fernet.generate_key().decode("ascii"))
    ct = v1.encrypt("crossed wires")
    with pytest.raises(InvalidToken):
        v2.decrypt(ct)


def test_construction_rejects_empty_key():
    with pytest.raises(ValueError, match="master_key_b64 is required"):
        SecretsVault("")


def test_encrypt_rejects_non_string():
    v = SecretsVault(_TEST_KEY)
    with pytest.raises(TypeError):
        v.encrypt(b"already bytes")  # type: ignore[arg-type]


def test_decrypt_rejects_non_bytes():
    v = SecretsVault(_TEST_KEY)
    with pytest.raises(TypeError):
        v.decrypt("not bytes")  # type: ignore[arg-type]


def test_vault_from_env_raises_clearly_when_unset(monkeypatch):
    monkeypatch.delenv("CHALYBCRYPTO_MASTER_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CHALYBCRYPTO_MASTER_ENCRYPTION_KEY is unset"):
        vault_from_env()


def test_vault_from_env_reads_configured_key(monkeypatch):
    monkeypatch.setenv("CHALYBCRYPTO_MASTER_ENCRYPTION_KEY", _TEST_KEY)
    v = vault_from_env()
    # Round-trip proves the key was used.
    assert v.decrypt(v.encrypt("ok")) == "ok"


def test_unicode_plaintext_round_trips():
    """API keys can contain weird chars; make sure UTF-8 round-trips cleanly."""
    v = SecretsVault(_TEST_KEY)
    sample = "API-token-π-ñoño-🦀"
    assert v.decrypt(v.encrypt(sample)) == sample
