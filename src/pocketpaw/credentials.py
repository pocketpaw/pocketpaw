"""Encrypted credential storage for PocketPaw.

Changes:
  - 2026-02-06: Initial implementation — Fernet encryption with machine-derived PBKDF2 key.
  - 2026-04-03: Hardened: Argon2id + AES-256-GCM, backup-safe migration, AEAD.

Stores API keys and tokens in ~/.pocketpaw/secrets.enc instead of plaintext config.json.
Encryption key derived from machine identity (hostname + MAC + username) so the encrypted
file only works on the same machine/user. Salt stored in ~/.pocketpaw/.salt.
"""

import base64
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# Fields that are considered secrets and must be stored encrypted.
SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "telegram_bot_token",
        "openai_api_key",
        "anthropic_api_key",
        "openai_compatible_api_key",
        "openrouter_api_key",
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
        "sarvam_api_key",
        "litellm_api_key",
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


VERSION_2_HEADER = b"PAW\x02"


class CredentialMigrationError(Exception):
    """Raised when v1 → v2 migration fails and cannot be recovered."""


class CredentialStore:
    """Encrypted credential store (2026 Edition).

    Supports:
      - v1: Fernet + PBKDF2 (legacy)
      - v2: AES-256-GCM + Argon2id (modern standard)

    Storage:
      - ~/.pocketpaw/secrets.enc  (Encrypted JSON)
      - ~/.pocketpaw/.salt        (16-byte random salt)
    """

    def __init__(self, config_dir: Path | None = None):
        if config_dir is None:
            config_dir = Path.home() / ".pocketpaw"
        self._config_dir = config_dir
        self._secrets_path = config_dir / "secrets.enc"
        self._salt_path = config_dir / ".salt"
        self._cache: dict[str, str] | None = None

    def _get_machine_id(self) -> str:
        """Return a persistent machine identifier.

        Tries (in order):
          1. /etc/machine-id  (Linux — systemd)
          2. /var/lib/dbus/machine-id  (Linux — older dbus)
          3. platform.node()  (hostname — fallback)

        uuid.getnode() is intentionally NOT used because it returns a
        random MAC on systems without a discoverable NIC, producing a
        different value on every process start.
        """
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                mid = Path(p).read_text().strip()
                if mid:
                    return mid
            except OSError:
                continue
        return platform.node()

    def _get_macos_hardware_uuid(self) -> str:
        """Extract macOS hardware UUID or fallback to system identifier."""

        # 1. Use stable constant for CI/GitHub Actions
        if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI") == "true":
            return "CI_ENVIRONMENT_ID"

        # 2. Only attempt ioreg on macOS
        if sys.platform != "darwin":
            # Fallback for Linux or Windows
            return "NO_HARDWARE_UUID"

        try:
            # Command to extract the Hardware UUID on macOS (safer version without shell=True)
            output = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], text=True
            )
            for line in output.splitlines():
                if "IOPlatformUUID" in line:
                    # Line looks like: "IOPlatformUUID" = "6D7AA782-..."
                    parts = line.split("=")
                    if len(parts) > 1:
                        return parts[1].strip().strip('"')
            return "NO_HARDWARE_UUID"
        except Exception:
            return "NO_HARDWARE_UUID"  # Last resort fallback

    # -----------------------------------------------------------------
    # Identity helpers
    # -----------------------------------------------------------------

    def _get_v1_machine_identity(self) -> bytes:
        """Build the v1 machine-bound identity string (machine_id|login_name).

        This MUST exactly match the original _get_machine_identity()
        from before the v2 upgrade so that legacy Fernet files can be
        decrypted correctly during migration.
        """
        parts = [self._get_machine_id()]
        try:
            parts.append(os.getlogin())
        except OSError:
            parts.append(os.environ.get("USER", os.environ.get("USERNAME", "pocketpaw")))
        return "|".join(parts).encode("utf-8")

    def _get_machine_identity(self) -> bytes:
        """Build a v2 machine-bound identity string (machine_id|hw_uuid|login_name)."""
        parts = [
            self._get_machine_id(),
            self._get_macos_hardware_uuid(),
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

    # -----------------------------------------------------------------
    # Key derivation
    # -----------------------------------------------------------------

    def _derive_key(self) -> bytes:
        """Derive a legacy Fernet key from v1 machine identity + salt via PBKDF2."""
        salt = self._get_or_create_salt()
        identity = self._get_v1_machine_identity()

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480_000,
        )
        raw_key = kdf.derive(identity)
        return base64.urlsafe_b64encode(raw_key)

    def _derive_key_v2(self, salt: bytes) -> bytes:
        """Derive an AES-256 key via Argon2id (2026 standard)."""
        kdf = Argon2id(
            salt=salt,
            length=32,
            iterations=3,
            memory_cost=65536,
            lanes=4,
        )
        return kdf.derive(self._get_machine_identity())

    # -----------------------------------------------------------------
    # Load / Save
    # -----------------------------------------------------------------

    def _load(self) -> dict[str, str]:
        """Decrypt and load secrets from disk with auto-migration support."""
        if self._cache is not None:
            return self._cache

        if not self._secrets_path.exists():
            self._cache = {}
            return self._cache

        raw_data = self._secrets_path.read_bytes()
        if not raw_data:
            self._cache = {}
            return self._cache

        # ---- v2: AES-256-GCM + Argon2id ----
        if raw_data.startswith(VERSION_2_HEADER):
            try:
                nonce = raw_data[len(VERSION_2_HEADER) : len(VERSION_2_HEADER) + 12]
                ciphertext = raw_data[len(VERSION_2_HEADER) + 12 :]
                aesgcm = AESGCM(self._derive_key_v2(self._get_or_create_salt()))
                decrypted = aesgcm.decrypt(nonce, ciphertext, VERSION_2_HEADER)
                self._cache = json.loads(decrypted.decode("utf-8"))
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt v2 secrets.enc (machine changed?): %s. "
                    "Starting with empty credential store.",
                    exc,
                )
                self._cache = {}
            return self._cache

        # ---- v1: Legacy Fernet + PBKDF2 (migration path) ----
        if raw_data.startswith(b"gAAAA"):
            backup_path = self._secrets_path.with_suffix(".enc.v1.bak")
            try:
                # 1. Create backup BEFORE any mutation
                shutil.copy2(self._secrets_path, backup_path)
                _ensure_permissions(backup_path)

                # 2. Decrypt with the original v1 key derivation
                fernet = Fernet(self._derive_key())
                decrypted = fernet.decrypt(raw_data)
                data = json.loads(decrypted.decode("utf-8"))

                # 3. Re-encrypt as v2
                logger.info("Auto-migrating secrets.enc to v2 security format.")
                self._save(data)

                # 4. Test-decrypt the newly written v2 file to be sure
                v2_raw = self._secrets_path.read_bytes()
                v2_nonce = v2_raw[len(VERSION_2_HEADER) : len(VERSION_2_HEADER) + 12]
                v2_ct = v2_raw[len(VERSION_2_HEADER) + 12 :]
                aesgcm = AESGCM(self._derive_key_v2(self._get_or_create_salt()))
                aesgcm.decrypt(v2_nonce, v2_ct, VERSION_2_HEADER)

                self._cache = data
                return self._cache

            except InvalidToken:
                # v1 decryption failed — restore backup, raise immediately
                if backup_path.exists():
                    shutil.copy2(backup_path, self._secrets_path)
                    logger.error(
                        "v1 secret decryption failed (wrong key?). Backup restored to secrets.enc."
                    )
                raise CredentialMigrationError(
                    "Failed to decrypt v1 secrets.enc — key derivation mismatch. "
                    "The backup has been restored."
                ) from None

            except Exception as exc:
                # Migration or v2 test-decrypt failed — restore backup
                if backup_path.exists():
                    shutil.copy2(backup_path, self._secrets_path)
                    logger.error(
                        "v1→v2 migration failed (%s). Backup restored to secrets.enc.",
                        exc,
                    )
                raise CredentialMigrationError(
                    f"v1→v2 migration failed: {exc}. The backup has been restored."
                ) from exc

        # ---- Unknown format ----
        logger.warning(
            "secrets.enc has unrecognised format (first 4 bytes: %r). "
            "Starting with empty credential store.",
            raw_data[:4],
        )
        self._cache = {}
        return self._cache

    def _save(self, data: dict[str, str]) -> None:
        """Encrypt and write secrets to disk using v2 (AES-256-GCM + Argon2id)."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        _ensure_dir_permissions(self._config_dir)

        salt = self._get_or_create_salt()
        nonce = os.urandom(12)
        aesgcm = AESGCM(self._derive_key_v2(salt))

        plaintext = json.dumps(data).encode("utf-8")
        ciphertext = aesgcm.encrypt(nonce, plaintext, VERSION_2_HEADER)

        # Build v2 payload: [HEADER][NONCE][CIPHERTEXT]
        payload = VERSION_2_HEADER + nonce + ciphertext

        self._secrets_path.write_bytes(payload)
        _ensure_permissions(self._secrets_path)
        self._cache = data

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

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
