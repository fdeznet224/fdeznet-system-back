from src.infrastructure.encrypted_field import (
    PREFIX,
    decrypt_field_secret,
    encrypt_field_secret,
)


def test_infrastructure_secret_is_encrypted_and_reversible(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-with-enough-entropy")
    encrypted = encrypt_field_secret("router-password")

    assert encrypted.startswith(PREFIX)
    assert "router-password" not in encrypted
    assert decrypt_field_secret(encrypted) == "router-password"


def test_legacy_plaintext_remains_readable_during_migration(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-with-enough-entropy")
    assert decrypt_field_secret("legacy-password") == "legacy-password"
