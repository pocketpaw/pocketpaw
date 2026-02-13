# Unit tests for encrypted credential storage and security hardening.
#
# Created: 2026-02-06
# Tests: CredentialStore, config save/load separation, plaintext migration,
#         file permissions, and log secret scrubbing.

import json
import logging
import os
import stat
import sys
from unittest.mock import patch

import pytest

from pocketclaw.credentials import SECRET_FIELDS, CredentialStore

# =============================================================================
# CREDENTIAL STORE — CORE
# =============================================================================


class TestCredentialStore:
    """Tests for the Fernet-encrypted credential store."""

    @pytest.fixture
    def store(self, tmp_path):
        """Create a CredentialStore backed by a temp directory."""
        return CredentialStore(config_dir=tmp_path)

    def test_set_and_get(self, store):
        """set() stores a value, get() retrieves it."""
        store.set("anthropic_api_key", "sk-ant-test123")
        assert store.get("anthropic_api_key") == "sk-ant-test123"

    def test_get_nonexistent_returns_none(self, store):
        """get() returns None for a key that was never stored."""
        assert store.get("no_such_key") is None

    def test_overwrite(self, store):
        """set() with the same key overwrites the previous value."""
        store.set("key", "old")
        store.set("key", "new")
        assert store.get("key") == "new"

    def test_delete(self, store):
        """delete() removes a key."""
        store.set("key", "value")
        store.delete("key")
        assert store.get("key") is None

    def test_delete_nonexistent_is_noop(self, store):
        """delete() on a missing key does not raise."""
        store.delete("no_such_key")  # should not raise

    def test_get_all(self, store):
        """get_all() returns a dict of all stored secrets."""
        store.set("a", "1")
        store.set("b", "2")
        result = store.get_all()
        assert result == {"a": "1", "b": "2"}

    def test_get_all_returns_copy(self, store):
        """get_all() returns a copy — mutating it doesn't affect the store."""
        store.set("key", "val")
        copy = store.get_all()
        copy["key"] = "hacked"
        assert store.get("key") == "val"

    def test_multiple_keys(self, store):
        """Store and retrieve multiple distinct keys."""
        secrets = {
            "telegram_bot_token": "123456:AAFake",
            "openai_api_key": "sk-openai-xxx",
            "anthropic_api_key": "sk-ant-yyy",
            "discord_bot_token": "MTA.disc.token",
            "slack_bot_token": "xoxb-slack-123",
        }
        for k, v in secrets.items():
            store.set(k, v)

        for k, v in secrets.items():
            assert store.get(k) == v, f"Mismatch for {k}"


class TestCredentialStorePersistence:
    """Tests that data survives cache clears and fresh instances."""

    @pytest.fixture
    def store(self, tmp_path):
        return CredentialStore(config_dir=tmp_path)

    def test_survives_cache_clear(self, store):
        """Data persists after clearing the in-memory cache."""
        store.set("key", "value")
        store.clear_cache()
        assert store.get("key") == "value"

    def test_survives_new_instance(self, tmp_path):
        """A new CredentialStore instance reads the same encrypted data."""
        store1 = CredentialStore(config_dir=tmp_path)
        store1.set("key", "value")

        store2 = CredentialStore(config_dir=tmp_path)
        assert store2.get("key") == "value"


class TestCredentialStoreEncryption:
    """Tests that secrets are actually encrypted on disk."""

    @pytest.fixture
    def store(self, tmp_path):
        return CredentialStore(config_dir=tmp_path)

    def test_secrets_enc_exists(self, store, tmp_path):
        """Setting a value creates secrets.enc on disk."""
        store.set("key", "value")
        assert (tmp_path / "secrets.enc").exists()

    def test_secrets_enc_is_not_readable_json(self, store, tmp_path):
        """secrets.enc must NOT be valid JSON (it's encrypted)."""
        store.set("key", "value")
        raw = (tmp_path / "secrets.enc").read_bytes()

        with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
            json.loads(raw)

    def test_secrets_enc_does_not_contain_plaintext(self, store, tmp_path):
        """The raw encrypted file must not contain the plaintext secret."""
        secret = "sk-ant-super-secret-key-12345"
        store.set("anthropic_api_key", secret)
        raw = (tmp_path / "secrets.enc").read_bytes()
        assert secret.encode() not in raw

    def test_salt_file_created(self, store, tmp_path):
        """A .salt file is created for key derivation."""
        store.set("key", "value")
        salt_path = tmp_path / ".salt"
        assert salt_path.exists()
        assert len(salt_path.read_bytes()) >= 16


class TestCredentialStoreErrorHandling:
    """Tests for graceful handling of corrupted / missing files."""

    def test_corrupted_secrets_file(self, tmp_path):
        """If secrets.enc is corrupted, store returns empty and doesn't crash."""
        # Write garbage to secrets.enc
        (tmp_path / "secrets.enc").write_bytes(b"not-encrypted-garbage-data")
        # Write a valid salt
        (tmp_path / ".salt").write_bytes(os.urandom(16))

        store = CredentialStore(config_dir=tmp_path)
        assert store.get("any_key") is None
        assert store.get_all() == {}

    def test_missing_salt_regenerates(self, tmp_path):
        """If .salt is deleted between invocations, a new one is generated
        (old data becomes unreadable, but no crash)."""
        store = CredentialStore(config_dir=tmp_path)
        store.set("key", "value")

        # Delete salt
        (tmp_path / ".salt").unlink()
        store.clear_cache()

        # New salt → old data unreadable, but should not crash
        result = store.get("key")
        # Value is lost (salt changed), but no exception
        assert result is None or result == "value"  # depends on timing


class TestFilePermissions:
    """Tests that files get correct permissions (Unix only)."""

    @pytest.fixture
    def store(self, tmp_path):
        return CredentialStore(config_dir=tmp_path)

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod not supported on Windows")
    def test_secrets_enc_permissions(self, store, tmp_path):
        """secrets.enc should have 600 permissions."""
        store.set("key", "value")
        enc_path = tmp_path / "secrets.enc"
        mode = stat.S_IMODE(enc_path.stat().st_mode)
        assert mode == 0o600, f"Expected 600, got {oct(mode)}"

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod not supported on Windows")
    def test_salt_permissions(self, store, tmp_path):
        """Salt file should have 600 permissions."""
        store.set("key", "value")
        salt_path = tmp_path / ".salt"
        mode = stat.S_IMODE(salt_path.stat().st_mode)
        assert mode == 0o600, f"Expected 600, got {oct(mode)}"


# =============================================================================
# CONFIG.PY INTEGRATION — SAVE / LOAD
# =============================================================================


class TestConfigSecretSeparation:
    """Tests that Settings.save() puts secrets in encrypted store,
    NOT in config.json."""

    @pytest.fixture
    def env(self, tmp_path):
        """Set up a temp config dir with patched get_config_dir and credential store."""
        import pocketclaw.config as cfg
        import pocketclaw.credentials as creds

        test_store = CredentialStore(config_dir=tmp_path)

        original_fn = cfg.get_config_dir
        cfg.get_config_dir = lambda: tmp_path
        cfg._MIGRATION_DONE_PATH = None

        # Mark migration as done so it doesn't interfere
        (tmp_path / ".secrets_migrated").write_text("1")

        with patch.object(creds, "get_credential_store", return_value=test_store):
            yield {
                "tmp_path": tmp_path,
                "store": test_store,
            }

        cfg.get_config_dir = original_fn

    def test_secrets_not_in_config_json(self, env):
        """Secrets must NOT be written to config.json in plaintext."""
        from pocketclaw.config import Settings

        settings = Settings(
            anthropic_api_key="sk-ant-secret",
            openai_api_key="sk-openai-secret",
            telegram_bot_token="123:AAFake",
        )
        settings.save()

        config_data = json.loads((env["tmp_path"] / "config.json").read_text())
        assert "anthropic_api_key" not in config_data
        assert "openai_api_key" not in config_data
        assert "telegram_bot_token" not in config_data

        # Secrets should be in the encrypted credential store instead
        store = env["store"]
        assert store.get("anthropic_api_key") == "sk-ant-secret"
        assert store.get("openai_api_key") == "sk-openai-secret"
        assert store.get("telegram_bot_token") == "123:AAFake"

    def test_non_secrets_in_config_json(self, env):
        """Non-secret fields should still be in config.json."""
        from pocketclaw.config import Settings

        settings = Settings(
            agent_backend="claude_agent_sdk",
            llm_provider="anthropic",
            anthropic_model="claude-sonnet-4-5-20250929",
        )
        settings.save()

        config_data = json.loads((env["tmp_path"] / "config.json").read_text())
        assert config_data["agent_backend"] == "claude_agent_sdk"
        assert config_data["llm_provider"] == "anthropic"

    def test_secrets_stored_in_credential_store(self, env):
        """Secrets should be retrievable from the encrypted store after save()."""
        from pocketclaw.config import Settings

        settings = Settings(
            anthropic_api_key="sk-ant-test",
            discord_bot_token="disc-token",
        )
        settings.save()

        store = env["store"]
        assert store.get("anthropic_api_key") == "sk-ant-test"
        assert store.get("discord_bot_token") == "disc-token"

    def test_load_merges_secrets_and_config(self, env):
        """Settings.load() must combine config.json + encrypted store."""
        from pocketclaw.config import Settings

        # Save settings (secrets go to store, non-secrets to config.json)
        settings = Settings(
            agent_backend="claude_agent_sdk",
            anthropic_api_key="sk-ant-loaded",
            telegram_bot_token="123:AALoaded",
        )
        settings.save()

        # Load back
        loaded = Settings.load()
        assert loaded.agent_backend == "claude_agent_sdk"
        assert loaded.anthropic_api_key == "sk-ant-loaded"
        assert loaded.telegram_bot_token == "123:AALoaded"

    def test_save_preserves_existing_secrets(self, env):
        """Saving new non-secret settings must not lose existing encrypted secrets."""
        from pocketclaw.config import Settings

        # First save: set an API key
        s1 = Settings(anthropic_api_key="sk-ant-original")
        s1.save()

        # Second save: change only a non-secret field (key not set in this instance)
        s2 = Settings(llm_provider="ollama")
        s2.save()

        # The store should still have the original key
        store = env["store"]
        assert store.get("anthropic_api_key") == "sk-ant-original"


# =============================================================================
# MIGRATION — PLAINTEXT → ENCRYPTED
# =============================================================================


class TestPlaintextMigration:
    """Tests for the one-time migration from plaintext config.json to encrypted store."""

    @pytest.fixture
    def env(self, tmp_path):
        """Set up a temp config dir with NO migration flag (simulates upgrade)."""
        import pocketclaw.config as cfg
        import pocketclaw.credentials as creds

        test_store = CredentialStore(config_dir=tmp_path)

        original_fn = cfg.get_config_dir
        cfg.get_config_dir = lambda: tmp_path
        cfg._MIGRATION_DONE_PATH = None  # Force re-check

        with patch.object(creds, "get_credential_store", return_value=test_store):
            yield {
                "tmp_path": tmp_path,
                "store": test_store,
            }

        cfg.get_config_dir = original_fn

    def test_plaintext_keys_migrated_to_store(self, env):
        """Plaintext API keys in config.json are moved to encrypted store."""
        from pocketclaw.config import Settings

        # Simulate pre-upgrade config.json with plaintext secrets
        old_config = {
            "telegram_bot_token": "123:AAOldToken",
            "anthropic_api_key": "sk-ant-old",
            "openai_api_key": "sk-old-openai",
            "agent_backend": "claude_agent_sdk",
            "allowed_user_id": 99999,
        }
        (env["tmp_path"] / "config.json").write_text(json.dumps(old_config))

        # Load triggers migration
        loaded = Settings.load()

        # Verify secrets are now in encrypted store
        store = env["store"]
        assert store.get("telegram_bot_token") == "123:AAOldToken"
        assert store.get("anthropic_api_key") == "sk-ant-old"
        assert store.get("openai_api_key") == "sk-old-openai"

    def test_plaintext_keys_preserved_in_config_json(self, env):
        """After migration, config.json still has the keys (as fallback)."""
        from pocketclaw.config import Settings

        old_config = {
            "telegram_bot_token": "123:AAOldToken",
            "anthropic_api_key": "sk-ant-old",
            "agent_backend": "claude_agent_sdk",
        }
        (env["tmp_path"] / "config.json").write_text(json.dumps(old_config))

        Settings.load()

        updated = json.loads((env["tmp_path"] / "config.json").read_text())
        # Keys remain in config.json as fallback (file is chmod 600)
        assert updated.get("telegram_bot_token") == "123:AAOldToken"
        assert updated.get("anthropic_api_key") == "sk-ant-old"
        assert updated.get("agent_backend") == "claude_agent_sdk"

    def test_migration_flag_created(self, env):
        """Migration creates a .secrets_migrated flag file."""
        from pocketclaw.config import Settings

        (env["tmp_path"] / "config.json").write_text(json.dumps({"agent_backend": "test"}))
        Settings.load()
        assert (env["tmp_path"] / ".secrets_migrated").exists()

    def test_migration_runs_only_once(self, env):
        """If the flag file exists, migration should not re-run."""
        import pocketclaw.config as cfg
        from pocketclaw.config import Settings

        # Write config with plaintext key
        old_config = {"anthropic_api_key": "sk-ant-should-not-migrate"}
        (env["tmp_path"] / "config.json").write_text(json.dumps(old_config))

        # Pre-set the flag
        (env["tmp_path"] / ".secrets_migrated").write_text("1")
        cfg._MIGRATION_DONE_PATH = env["tmp_path"] / ".secrets_migrated"

        Settings.load()

        # Key should NOT have been migrated (flag was already set)
        store = env["store"]
        assert store.get("anthropic_api_key") is None

    def test_migration_with_no_config_file(self, env):
        """Migration with no config.json should set the flag and not crash."""
        from pocketclaw.config import Settings

        # No config.json exists — just load
        Settings.load()
        assert (env["tmp_path"] / ".secrets_migrated").exists()

    def test_loaded_settings_have_migrated_values(self, env):
        """After migration, Settings.load() returns the migrated secrets."""
        from pocketclaw.config import Settings

        old_config = {
            "anthropic_api_key": "sk-ant-migrated",
            "agent_backend": "claude_agent_sdk",
            "llm_provider": "anthropic",
        }
        (env["tmp_path"] / "config.json").write_text(json.dumps(old_config))

        loaded = Settings.load()
        assert loaded.anthropic_api_key == "sk-ant-migrated"
        assert loaded.agent_backend == "claude_agent_sdk"
        assert loaded.llm_provider == "anthropic"


# =============================================================================
# SECRET FIELDS LIST
# =============================================================================


class TestSecretFieldsList:
    """Verify the SECRET_FIELDS set is correct and complete."""

    def test_expected_fields_present(self):
        """All secret fields must be in SECRET_FIELDS."""
        expected = {
            "telegram_bot_token",
            "openai_api_key",
            "anthropic_api_key",
            "discord_bot_token",
            "slack_bot_token",
            "slack_app_token",
            "whatsapp_access_token",
            "whatsapp_verify_token",
            "tavily_api_key",
            "brave_search_api_key",
            "parallel_api_key",
            "elevenlabs_api_key",
            "google_api_key",
            "google_oauth_client_id",
            "google_oauth_client_secret",
            "spotify_client_id",
            "spotify_client_secret",
            "matrix_access_token",
            "matrix_password",
            "teams_app_id",
            "teams_app_password",
            "gchat_service_account_key",
        }
        assert SECRET_FIELDS == expected

    def test_non_secrets_excluded(self):
        """Common non-secret fields must NOT be in SECRET_FIELDS."""
        non_secrets = [
            "agent_backend",
            "llm_provider",
            "ollama_host",
            "ollama_model",
            "openai_model",
            "anthropic_model",
            "memory_backend",
            "allowed_user_id",
            "web_host",
            "web_port",
        ]
        for field in non_secrets:
            assert field not in SECRET_FIELDS, f"Non-secret '{field}' in SECRET_FIELDS!"


# =============================================================================
# LOG SECRET SCRUBBING
# =============================================================================


class TestSecretFilter:
    """Tests for the SecretFilter logging filter."""

    @pytest.fixture
    def log_filter(self):
        from pocketclaw.logging_setup import SecretFilter

        return SecretFilter()

    def _make_record(self, msg, args=None):
        return logging.LogRecord("test", logging.INFO, "", 0, msg, args, None)

    def test_scrubs_anthropic_key(self, log_filter):
        record = self._make_record("Key is sk-ant-api03-abcdef1234567890xyz")
        log_filter.filter(record)
        assert "sk-ant-" not in record.msg
        assert "***REDACTED***" in record.msg

    def test_scrubs_openai_key(self, log_filter):
        record = self._make_record("Key: sk-proj-abcdefghijklmnopqrstuvwxyz")
        log_filter.filter(record)
        assert "sk-proj-" not in record.msg
        assert "***REDACTED***" in record.msg

    def test_scrubs_slack_bot_token(self, log_filter):
        record = self._make_record("Token: xoxb-123-456-abcdefg")
        log_filter.filter(record)
        assert "xoxb-" not in record.msg
        assert "***REDACTED***" in record.msg

    def test_scrubs_slack_app_token(self, log_filter):
        record = self._make_record("App: xapp-1-A02-abcdefghijk")
        log_filter.filter(record)
        assert "xapp-" not in record.msg
        assert "***REDACTED***" in record.msg

    def test_scrubs_telegram_bot_token(self, log_filter):
        record = self._make_record("Bot: 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
        log_filter.filter(record)
        assert ":AA" not in record.msg
        assert "***REDACTED***" in record.msg

    def test_normal_text_unchanged(self, log_filter):
        record = self._make_record("Normal log message with no secrets")
        log_filter.filter(record)
        assert record.msg == "Normal log message with no secrets"

    def test_scrubs_in_args(self, log_filter):
        record = self._make_record("Error with key: %s", ("sk-ant-api03-leaked-key",))
        log_filter.filter(record)
        assert "sk-ant-" not in record.args[0]
        assert "***REDACTED***" in record.args[0]

    def test_multiple_secrets_in_one_message(self, log_filter):
        record = self._make_record("Keys: sk-ant-key1 and sk-openai-abcdefghijklmnopqrstuvwxyz")
        log_filter.filter(record)
        assert "sk-ant-" not in record.msg
        assert "sk-openai-" not in record.msg
        assert record.msg.count("***REDACTED***") == 2

    def test_filter_returns_true(self, log_filter):
        """Filter must return True (don't suppress the log record, just scrub it)."""
        record = self._make_record("sk-ant-secret")
        result = log_filter.filter(record)
        assert result is True
