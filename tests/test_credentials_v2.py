# Tests for the v2 CredentialStore hardening (AES-256-GCM + Argon2id).
#
# Covers:
#   - v2 encrypt/decrypt round-trip
#   - v1 → v2 migration with backup
#   - Migration failure + backup restoration
#   - AEAD (associated data) enforcement
#   - Unknown / corrupt format handling
#   - v1 identity backward-compatibility
#   - Cross-platform hardware UUID fallbacks

import base64
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from pocketpaw.credentials import (
    VERSION_2_HEADER,
    CredentialMigrationError,
    CredentialStore,
)

# ============================================================================
# Helpers — create a real v1 (Fernet) encrypted file using the ORIGINAL logic
# ============================================================================


def _build_v1_identity(store: CredentialStore) -> bytes:
    """Reproduce the ORIGINAL v1 identity: machine_id|login_name."""
    parts = [store._get_machine_id()]
    try:
        parts.append(os.getlogin())
    except OSError:
        parts.append(os.environ.get("USER", os.environ.get("USERNAME", "pocketpaw")))
    return "|".join(parts).encode("utf-8")


def _write_v1_secrets(tmp_path: Path, store: CredentialStore, data: dict) -> None:
    """Write a legitimate v1 Fernet-encrypted secrets.enc file."""
    salt = store._get_or_create_salt()
    identity = _build_v1_identity(store)

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480_000,
    )
    raw_key = kdf.derive(identity)
    fernet_key = base64.urlsafe_b64encode(raw_key)

    fernet = Fernet(fernet_key)
    plaintext = json.dumps(data).encode("utf-8")
    encrypted = fernet.encrypt(plaintext)

    (tmp_path / "secrets.enc").write_bytes(encrypted)


# ============================================================================
# V2 ROUND-TRIP
# ============================================================================


class TestV2RoundTrip:
    """AES-256-GCM + Argon2id encrypt → decrypt cycle."""

    @pytest.fixture
    def store(self, tmp_path):
        return CredentialStore(config_dir=tmp_path)

    def test_basic_round_trip(self, store):
        """set() then get() returns the same value."""
        store.set("api_key", "sk-test-12345")
        store.clear_cache()
        assert store.get("api_key") == "sk-test-12345"

    def test_multiple_keys_round_trip(self, store):
        """Multiple keys survive a cache-clear round trip."""
        secrets = {
            "anthropic_api_key": "sk-ant-xxx",
            "openai_api_key": "sk-openai-yyy",
            "telegram_bot_token": "123:AAFake",
        }
        for k, v in secrets.items():
            store.set(k, v)
        store.clear_cache()
        for k, v in secrets.items():
            assert store.get(k) == v

    def test_v2_header_present(self, store, tmp_path):
        """Encrypted file starts with the PAW\\x02 header."""
        store.set("key", "value")
        raw = (tmp_path / "secrets.enc").read_bytes()
        assert raw.startswith(VERSION_2_HEADER)

    def test_new_instance_reads_v2(self, tmp_path):
        """A fresh CredentialStore instance can read v2 data."""
        store1 = CredentialStore(config_dir=tmp_path)
        store1.set("key", "persist")

        store2 = CredentialStore(config_dir=tmp_path)
        assert store2.get("key") == "persist"


# ============================================================================
# AEAD — Associated Data enforcement
# ============================================================================


class TestAEAD:
    """Ensure the version header is bound as associated data."""

    @pytest.fixture
    def store(self, tmp_path):
        return CredentialStore(config_dir=tmp_path)

    def test_aad_used_in_encryption(self, store, tmp_path):
        """Decrypting with wrong AAD (None instead of header) must fail."""
        store.set("key", "value")
        raw = (tmp_path / "secrets.enc").read_bytes()

        nonce = raw[len(VERSION_2_HEADER) : len(VERSION_2_HEADER) + 12]
        ciphertext = raw[len(VERSION_2_HEADER) + 12 :]

        salt = store._get_or_create_salt()
        aesgcm = AESGCM(store._derive_key_v2(salt))

        # Correct AAD works
        plaintext = aesgcm.decrypt(nonce, ciphertext, VERSION_2_HEADER)
        assert b"key" in plaintext

        # Wrong AAD (None) must fail
        with pytest.raises(Exception):
            aesgcm.decrypt(nonce, ciphertext, None)

    def test_tampered_header_fails(self, store, tmp_path):
        """Replacing the header bytes in the file causes decrypt failure."""
        store.set("key", "value")
        raw = (tmp_path / "secrets.enc").read_bytes()

        # Swap header from PAW\x02 to PAW\x03
        tampered = b"PAW\x03" + raw[4:]
        (tmp_path / "secrets.enc").write_bytes(tampered)

        # New store should fail to decrypt (unknown format, not PAW\x02)
        store2 = CredentialStore(config_dir=tmp_path)
        assert store2.get("key") is None
        assert store2.get_all() == {}


# ============================================================================
# V1 → V2 MIGRATION
# ============================================================================


class TestV1Migration:
    """v1 Fernet → v2 AES-GCM auto-migration."""

    @pytest.fixture
    def store(self, tmp_path):
        return CredentialStore(config_dir=tmp_path)

    def test_v1_migrates_to_v2(self, store, tmp_path):
        """A v1 file is transparently migrated and readable."""
        v1_data = {"anthropic_api_key": "sk-ant-legacy", "openai_api_key": "sk-old"}
        _write_v1_secrets(tmp_path, store, v1_data)

        # Load triggers migration
        assert store.get("anthropic_api_key") == "sk-ant-legacy"
        assert store.get("openai_api_key") == "sk-old"

        # File should now be v2
        raw = (tmp_path / "secrets.enc").read_bytes()
        assert raw.startswith(VERSION_2_HEADER)

    def test_migration_creates_backup(self, store, tmp_path):
        """A .v1.bak file is created during migration."""
        _write_v1_secrets(tmp_path, store, {"key": "value"})
        store.get("key")  # triggers migration

        backup = tmp_path / "secrets.enc.v1.bak"
        assert backup.exists()
        # Backup should contain the original Fernet data
        bak_data = backup.read_bytes()
        assert bak_data.startswith(b"gAAAA")

    def test_migration_preserves_all_data(self, store, tmp_path):
        """All keys from v1 are present in v2 after migration."""
        v1_data = {
            "telegram_bot_token": "123:AAFake",
            "slack_bot_token": "xoxb-test",
            "discord_bot_token": "disc-token",
        }
        _write_v1_secrets(tmp_path, store, v1_data)

        store.clear_cache()
        result = store.get_all()
        assert result == v1_data


class TestMigrationFailure:
    """Migration failure scenarios — backup restoration."""

    def test_bad_v1_key_restores_backup(self, tmp_path):
        """If v1 decryption fails, backup is restored and error is raised."""
        store = CredentialStore(config_dir=tmp_path)

        # Write a valid v1 file with one identity
        _write_v1_secrets(tmp_path, store, {"key": "secret"})
        (tmp_path / "secrets.enc").read_bytes()

        # Now create a *different* store whose _derive_key() returns a wrong key
        # by writing a different salt after the v1 file was encrypted
        (tmp_path / ".salt").write_bytes(os.urandom(16))

        store2 = CredentialStore(config_dir=tmp_path)
        with pytest.raises(CredentialMigrationError, match="key derivation mismatch"):
            store2._load()

        # secrets.enc should be restored from backup (= original v1 data)
        restored = (tmp_path / "secrets.enc").read_bytes()
        assert restored.startswith(b"gAAAA")


# ============================================================================
# CORRUPT / UNKNOWN FORMAT
# ============================================================================


class TestCorruptFormat:
    """Handling of corrupted or unknown file formats."""

    def test_random_bytes_returns_empty(self, tmp_path):
        """Completely random file contents → empty dict, no crash."""
        (tmp_path / "secrets.enc").write_bytes(os.urandom(64))
        (tmp_path / ".salt").write_bytes(os.urandom(16))

        store = CredentialStore(config_dir=tmp_path)
        assert store.get_all() == {}

    def test_empty_file_returns_empty(self, tmp_path):
        """Empty secrets.enc → empty dict."""
        (tmp_path / "secrets.enc").write_bytes(b"")
        store = CredentialStore(config_dir=tmp_path)
        assert store.get_all() == {}

    def test_truncated_v2_returns_empty(self, tmp_path):
        """v2 header with truncated/corrupt payload → empty dict."""
        # Write header + 12-byte nonce + 1 byte garbage
        (tmp_path / "secrets.enc").write_bytes(VERSION_2_HEADER + os.urandom(13))
        (tmp_path / ".salt").write_bytes(os.urandom(16))

        store = CredentialStore(config_dir=tmp_path)
        assert store.get_all() == {}


# ============================================================================
# V1 IDENTITY BACKWARD COMPATIBILITY
# ============================================================================


class TestV1IdentityCompat:
    """Ensure _get_v1_machine_identity() exactly matches the original logic."""

    @pytest.fixture
    def store(self, tmp_path):
        return CredentialStore(config_dir=tmp_path)

    def test_v1_identity_is_two_parts(self, store):
        """v1 identity must have exactly 2 pipe-separated parts: machine_id|login."""
        identity = store._get_v1_machine_identity().decode("utf-8")
        parts = identity.split("|")
        assert len(parts) == 2, f"Expected 2 parts, got {parts}"

    def test_v2_identity_is_three_parts(self, store):
        """v2 identity must have exactly 3 parts: machine_id|hw_uuid|login."""
        identity = store._get_machine_identity().decode("utf-8")
        parts = identity.split("|")
        assert len(parts) == 3, f"Expected 3 parts, got {parts}"

    def test_v1_and_v2_share_first_and_last_part(self, store):
        """v1 and v2 identities share machine_id (first) and login (last)."""
        v1 = store._get_v1_machine_identity().decode("utf-8").split("|")
        v2 = store._get_machine_identity().decode("utf-8").split("|")
        assert v1[0] == v2[0], "machine_id differs"
        assert v1[1] == v2[2], "login_name differs"


# ============================================================================
# CROSS-PLATFORM HARDWARE UUID FALLBACKS
# ============================================================================


class TestHardwareUUIDFallbacks:
    """Ensure _get_macos_hardware_uuid() behaves correctly per platform."""

    @pytest.fixture
    def store(self, tmp_path):
        return CredentialStore(config_dir=tmp_path)

    def test_ci_environment_returns_constant(self, store):
        """CI=true → 'CI_ENVIRONMENT_ID' (stable, reproducible)."""
        with patch.dict(os.environ, {"CI": "true"}, clear=False):
            assert store._get_macos_hardware_uuid() == "CI_ENVIRONMENT_ID"

    def test_github_actions_returns_constant(self, store):
        """GITHUB_ACTIONS=true → 'CI_ENVIRONMENT_ID'."""
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False):
            assert store._get_macos_hardware_uuid() == "CI_ENVIRONMENT_ID"

    @patch.dict(os.environ, {}, clear=False)
    def test_non_darwin_returns_fallback(self, store):
        """On non-macOS, ioreg is NOT called; returns NO_HARDWARE_UUID."""
        # Remove CI markers if present
        env_clean = {k: v for k, v in os.environ.items() if k not in ("CI", "GITHUB_ACTIONS")}
        with patch.dict(os.environ, env_clean, clear=True):
            with patch("pocketpaw.credentials.sys") as mock_sys:
                mock_sys.platform = "linux"
                result = store._get_macos_hardware_uuid()
                assert result == "NO_HARDWARE_UUID"

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only test")
    def test_darwin_returns_uuid_or_default(self, store):
        """On macOS (non-CI), returns either the real UUID or NO_HARDWARE_UUID."""
        # Ensure CI env vars are not set
        env_clean = {k: v for k, v in os.environ.items() if k not in ("CI", "GITHUB_ACTIONS")}
        with patch.dict(os.environ, env_clean, clear=True):
            result = store._get_macos_hardware_uuid()
            # Should be a real UUID or NO_HARDWARE_UUID fallback, not CI_ENVIRONMENT_ID
            assert result != "CI_ENVIRONMENT_ID"
            assert isinstance(result, str) and len(result) > 0
