"""Encrypted credential storage for PocketPaw.

This module centralizes encrypted storage of long-lived secrets such as API
keys and access tokens. It is intentionally minimal and focused on:

* keeping raw secrets **out** of plaintext config files and logs,
* binding encrypted secrets to a specific machine/user combination, and
* providing a simple dictionary-like interface for other modules.

Secrets listed in :data:`SECRET_FIELDS` are typically sourced from
configuration (`Settings`) or setup flows (e.g. web pairing) and are
persisted in ``~/.pocketpaw/secrets.enc`` instead of ``config.json``.
`Settings.save()` uses :data:`SECRET_FIELDS` to decide which fields must be
written through :class:`CredentialStore`.

Security model:

* Encryption key is derived from a machine identity (hostname + MAC + user)
  via PBKDF2, so `secrets.enc` is only usable on the same machine + OS user.
* A 16-byte random salt is stored in ``~/.pocketpaw/.salt`` to harden the
  PBKDF2 derivation.
* This module **never** logs or returns secret values; logs mention only
  field names, counts, or file paths.

Changes:
  - 2026-02-06: Initial implementation — Fernet encryption with machine-derived PBKDF2 key.

File layout:
  - ``~/.pocketpaw/secrets.enc``  (Fernet-encrypted JSON mapping field→value)
  - ``~/.pocketpaw/.salt``        (16-byte random salt, auto-generated)
"""

import base64
import json
import logging
import os
import platform
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger: logging.Logger = logging.getLogger(__name__)

# Fields that are considered secrets and must be stored encrypted.
SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "telegram_bot_token",
        "openai_api_key",
        "anthropic_api_key",
        "openai_compatible_api_key",
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
    }
)


def _ensure_permissions(path: Path, mode: int = 0o600) -> None:
    """Set strict file permissions (owner read/write only) if possible.

    On platforms where :func:`Path.chmod` is not effective (notably Windows),
    this function silently skips permission adjustments rather than failing.
    """
    if not path.exists():
        return
    try:
        path.chmod(mode)
    except OSError:
        # Windows doesn't support chmod the same way — skip silently
        pass


def _ensure_dir_permissions(path: Path) -> None:
    """Set strict directory permissions (owner rwx only) if possible."""
    _ensure_permissions(path, mode=0o700)


class CredentialStore:
    """Encrypted credential store backed by Fernet + PBKDF2.

    Storage:
      - ``~/.pocketpaw/secrets.enc``  (Fernet-encrypted JSON)
      - ``~/.pocketpaw/.salt``        (16-byte random salt, auto-generated)

    The encryption key is derived from:

    * ``platform.node()`` (hostname)
    * ``uuid.getnode()`` (MAC address)
    * ``os.getlogin()`` or a best-effort user identifier

    so the encrypted file is bound to the current machine and user account.
    Callers should treat this as a **local convenience store**, not a
    cross-machine secret management solution.
    """

    def __init__(self, config_dir: Path | None = None) -> None:
        """Create a new credential store bound to ``config_dir``.

        No I/O occurs during initialization; files are only created or read
        lazily on first access.

        Args:
            config_dir: Base configuration directory. Defaults to
                ``~/.pocketpaw`` when omitted.
        """
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

    def _get_machine_identity(self) -> bytes:
        """Build a machine-bound identity string used for key derivation.

        The resulting bytes are **not** persisted to disk; they are derived
        on demand from non-secret machine characteristics and used as input
        to PBKDF2.
        """
        parts = [
            self._get_machine_id(),
        ]
        try:
            parts.append(os.getlogin())
        except OSError:
            # Headless / CI environments may not have a login name
            parts.append(os.environ.get("USER", os.environ.get("USERNAME", "pocketpaw")))
        return "|".join(parts).encode("utf-8")

    def _get_or_create_salt(self) -> bytes:
        """Load existing salt or generate a new 16-byte salt.

        If a salt file already exists but has an unexpected length, a new
        16-byte salt is generated and written, and the old contents are
        effectively ignored. This may render previously encrypted data
        undecipherable, which is acceptable for this local store.
        """
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
        """Derive a Fernet key from machine identity + salt via PBKDF2.

        The derived key is **never** logged or persisted and is used only
        in-memory to construct a :class:`Fernet` instance.
        """
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
        """Decrypt and load secrets from disk into an in-memory cache.

        On decryption or decoding failure, this method logs a warning and
        returns an empty mapping instead of raising, so callers do not have
        to handle storage-level errors. In such cases, previously stored
        secrets are effectively ignored until the file is repaired or
        removed.
        """
        if self._cache is not None:
            return self._cache

        if not self._secrets_path.exists():
            self._cache = {}
            logger.debug("Credential store not found at %s; starting empty.", self._secrets_path)
            return self._cache

        try:
            fernet = Fernet(self._derive_key())
            encrypted = self._secrets_path.read_bytes()
            decrypted = fernet.decrypt(encrypted)
            self._cache = json.loads(decrypted)
            logger.debug(
                "Loaded %d credential(s) from encrypted store at %s.",
                len(self._cache),
                self._secrets_path,
            )
        except (InvalidToken, json.JSONDecodeError, Exception) as exc:
            logger.warning(
                "Failed to decrypt secrets.enc (machine changed? corrupted?): %s. "
                "Starting with empty credential store.",
                exc,
            )
            self._cache = {}

        return self._cache

    def _save(self, data: dict[str, str]) -> None:
        """Encrypt and write secrets to disk.

        Callers must ensure that ``data`` contains only string values; secret
        values are not logged or exposed. The in-memory cache is updated
        after a successful write.
        """
        self._config_dir.mkdir(parents=True, exist_ok=True)
        _ensure_dir_permissions(self._config_dir)

        fernet = Fernet(self._derive_key())
        plaintext = json.dumps(data).encode("utf-8")
        encrypted = fernet.encrypt(plaintext)
        self._secrets_path.write_bytes(encrypted)
        _ensure_permissions(self._secrets_path)
        self._cache = data
        logger.debug(
            "Persisted %d credential(s) to encrypted store at %s.",
            len(self._cache),
            self._secrets_path,
        )

    def get(self, name: str) -> str | None:
        """Get a secret by name.

        Returns:
            The secret value if present, otherwise ``None``. Callers should
            treat ``None`` as "not configured" and handle it gracefully.
        """
        data = self._load()
        value = data.get(name)
        if value is None:
            logger.debug("Credential '%s' not found in store.", name)
        else:
            logger.debug("Credential '%s' loaded from store.", name)
        return value

    def set(self, name: str, value: str) -> None:
        """Store or update a secret value.

        The value is written to the encrypted backing store and cached in
        memory. The secret string itself is **never** logged.
        """
        data = self._load()
        data[name] = value
        self._save(data)

    def delete(self, name: str) -> None:
        """Remove a secret if it exists in the store."""
        data = self._load()
        if name in data:
            del data[name]
            self._save(data)
            logger.debug("Credential '%s' removed from store.", name)

    def get_all(self) -> dict[str, str]:
        """Get a copy of all stored secrets.

        The returned mapping is a shallow copy; changes to it do not affect
        the underlying store until explicitly written back via :meth:`set`
        or :meth:`delete`.
        """
        data = dict(self._load())
        logger.debug("Returning copy of %d credential(s) from store.", len(data))
        return data

    def clear_cache(self) -> None:
        """Force re-read from disk on next access.

        This is primarily useful in tests or when external tools may have
        modified ``secrets.enc`` out-of-band.
        """
        self._cache = None


@lru_cache
def get_credential_store() -> CredentialStore:
    """Get the process-wide singleton :class:`CredentialStore` instance.

    The store is created lazily on first use and reused across the process.
    """
    return CredentialStore()
