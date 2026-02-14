"""Encrypted credential storage for PocketPaw.

Changes:
  - 2026-02-06: Initial implementation — Fernet encryption with machine-derived PBKDF2 key.

Stores API keys and tokens in ~/.pocketclaw/secrets.enc instead of plaintext config.json.
Encryption key derived from machine identity (hostname + MAC + username) so the encrypted
file only works on the same machine/user. Salt stored in ~/.pocketclaw/.salt.
"""

import base64
import json
import logging
import os
import platform
from functools import lru_cache
from pathlib import Path
from uuid import getnode

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# Fields that are considered secrets and must be stored encrypted.
SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "telegram_bot_token",
        "openai_api_key",
        "anthropic_api_key",
        "gemini_api_key",
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
)


def _ensure_permissions(path: Path, mode: int = 0o600) -> None:
    """Set strict file permissions (owner read/write only)."""
    if not path.exists():
        return
    try:
        path.chmod(mode)
    except OSError:
        # Windows doesn't support chmod the same way — skip silently
        pass


def _ensure_dir_permissions(path: Path) -> None:
    """Set strict directory permissions (owner rwx only)."""
    _ensure_permissions(path, mode=0o700)


class CredentialStore:
    """Encrypted credential store backed by Fernet + PBKDF2.

    Storage:
      - ~/.pocketclaw/secrets.enc  (Fernet-encrypted JSON)
      - ~/.pocketclaw/.salt        (16-byte random salt, auto-generated)

    The encryption key is derived from:
      platform.node() + uuid.getnode() + os.getlogin()
    so the file is bound to the current machine and user account.
    """

    def __init__(self, config_dir: Path | None = None):
        if config_dir is None:
            config_dir = Path.home() / ".pocketclaw"
        self._config_dir = config_dir
        self._secrets_path = config_dir / "secrets.enc"
        self._salt_path = config_dir / ".salt"
        self._cache: dict[str, str] | None = None

    def _get_machine_identity(self) -> bytes:
        """Build a machine-bound identity string."""
        parts = [
            platform.node(),
            str(getnode()),
        ]
        try:
            parts.append(os.getlogin())
        except OSError:
            # Headless / CI environments may not have a login name
            parts.append(os.environ.get("USER", os.environ.get("USERNAME", "pocketpaw")))
        return "|".join(parts).encode("utf-8")

    def _get_or_create_salt(self) -> bytes:
        """Load existing salt or generate a new 16-byte salt."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        _ensure_dir_permissions(self._config_dir)

        if self._salt_path.exists():
            salt = self._salt_path.read_bytes()
            if len(salt) >= 16:
                return salt[:16]

        salt = os.urandom(16)
        self._salt_path.write_bytes(salt)
        _ensure_permissions(self._salt_path)
        return salt

    def _derive_key(self) -> bytes:
        """Derive a Fernet key from machine identity + salt via PBKDF2."""
        salt = self._get_or_create_salt()
        identity = self._get_machine_identity()

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480_000,
        )
        raw_key = kdf.derive(identity)
        return base64.urlsafe_b64encode(raw_key)

    def _load(self) -> dict[str, str]:
        """Decrypt and load secrets from disk."""
        if self._cache is not None:
            return self._cache

        if not self._secrets_path.exists():
            self._cache = {}
            return self._cache

        try:
            fernet = Fernet(self._derive_key())
            encrypted = self._secrets_path.read_bytes()
            decrypted = fernet.decrypt(encrypted)
            self._cache = json.loads(decrypted)
        except (InvalidToken, json.JSONDecodeError, Exception) as exc:
            logger.warning(
                "Failed to decrypt secrets.enc (machine changed? corrupted?): %s. "
                "Starting with empty credential store.",
                exc,
            )
            self._cache = {}

        return self._cache

    def _save(self, data: dict[str, str]) -> None:
        """Encrypt and write secrets to disk."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        _ensure_dir_permissions(self._config_dir)

        fernet = Fernet(self._derive_key())
        plaintext = json.dumps(data).encode("utf-8")
        encrypted = fernet.encrypt(plaintext)
        self._secrets_path.write_bytes(encrypted)
        _ensure_permissions(self._secrets_path)
        self._cache = data

    def get(self, name: str) -> str | None:
        """Get a secret by name. Returns None if not found."""
        data = self._load()
        return data.get(name)

    def set(self, name: str, value: str) -> None:
        """Store a secret."""
        data = self._load()
        data[name] = value
        self._save(data)

    def delete(self, name: str) -> None:
        """Remove a secret."""
        data = self._load()
        if name in data:
            del data[name]
            self._save(data)

    def get_all(self) -> dict[str, str]:
        """Get a copy of all stored secrets."""
        return dict(self._load())

    def clear_cache(self) -> None:
        """Force re-read from disk on next access."""
        self._cache = None


@lru_cache
def get_credential_store() -> CredentialStore:
    """Get the singleton CredentialStore instance."""
    return CredentialStore()
