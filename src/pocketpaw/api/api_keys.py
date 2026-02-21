# API Key Manager — create, verify, revoke, rotate, list.
# Created: 2026-02-20
#
# API keys use the format pp_<32-char-random> for easy identification in logs.
# Only sha256 hashes are stored — plaintext is shown once at creation (like GitHub PATs).
# Storage: ~/.pocketpaw/api_keys.json

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_PREFIX = "pp_"
_KEY_LENGTH = 32  # Random part length


class APIKeyRecord(BaseModel):
    """Stored API key record (no plaintext)."""

    id: str
    name: str
    key_hash: str
    prefix: str  # First 8 chars for identification
    scopes: list[str]
    created_at: str
    last_used_at: str | None = None
    expires_at: str | None = None
    revoked: bool = False


# Valid scopes
VALID_SCOPES = frozenset(
    {
        "chat",
        "sessions",
        "settings:read",
        "settings:write",
        "channels",
        "memory",
        "admin",
    }
)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class APIKeyManager:
    """Manages API keys with file-based persistence."""

    def __init__(self, storage_path: Path | None = None):
        if storage_path is None:
            from pocketpaw.config import get_config_dir

            storage_path = get_config_dir() / "api_keys.json"
        self._path = storage_path

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, records: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(records, indent=2))
        # Restrict permissions
        try:
            self._path.chmod(0o600)
        except OSError:
            pass

    def create(
        self,
        name: str,
        scopes: list[str] | None = None,
        expires_at: str | None = None,
    ) -> tuple[APIKeyRecord, str]:
        """Create a new API key. Returns (record, plaintext_key).

        The plaintext key is returned only once — it cannot be retrieved later.
        """
        if scopes is None:
            scopes = ["chat", "sessions"]

        # Validate scopes
        invalid = set(scopes) - VALID_SCOPES
        if invalid:
            raise ValueError(f"Invalid scopes: {invalid}")

        # Generate key
        random_part = secrets.token_urlsafe(_KEY_LENGTH)
        plaintext = f"{_PREFIX}{random_part}"
        key_hash = _hash_key(plaintext)
        key_id = secrets.token_hex(8)

        record = APIKeyRecord(
            id=key_id,
            name=name,
            key_hash=key_hash,
            prefix=plaintext[:12],
            scopes=scopes,
            created_at=datetime.now(UTC).isoformat(),
            expires_at=expires_at,
        )

        records = self._load()
        records.append(record.model_dump())
        self._save(records)

        try:
            from pocketpaw.security.audit import get_audit_logger

            get_audit_logger().log_api_event(
                action="api_key_created",
                target=f"key:{key_id}",
                key_name=name,
                scopes=scopes,
            )
        except Exception:
            pass

        return record, plaintext

    def verify(self, key: str) -> APIKeyRecord | None:
        """Verify an API key. Returns the record if valid, None otherwise."""
        if not key.startswith(_PREFIX):
            return None

        key_hash = _hash_key(key)
        records = self._load()

        for rec_dict in records:
            rec = APIKeyRecord(**rec_dict)
            if hmac.compare_digest(rec.key_hash, key_hash) and not rec.revoked:
                # Check expiry
                if rec.expires_at:
                    exp = datetime.fromisoformat(rec.expires_at)
                    if datetime.now(UTC) > exp:
                        return None

                # Update last_used_at
                now = datetime.now(UTC).isoformat()
                rec_dict["last_used_at"] = now
                self._save(records)
                rec.last_used_at = now

                return rec

        return None

    def revoke(self, key_id: str) -> bool:
        """Revoke an API key by ID. Returns True if found and revoked."""
        records = self._load()
        for rec_dict in records:
            if rec_dict["id"] == key_id and not rec_dict["revoked"]:
                rec_dict["revoked"] = True
                self._save(records)
                try:
                    from pocketpaw.security.audit import get_audit_logger

                    get_audit_logger().log_api_event(
                        action="api_key_revoked",
                        target=f"key:{key_id}",
                        key_name=rec_dict.get("name", ""),
                    )
                except Exception:
                    pass
                return True
        return False

    def rotate(self, key_id: str) -> tuple[APIKeyRecord, str] | None:
        """Revoke an existing key and create a new one with the same name/scopes."""
        records = self._load()
        old_rec = None
        for rec_dict in records:
            if rec_dict["id"] == key_id and not rec_dict["revoked"]:
                old_rec = APIKeyRecord(**rec_dict)
                rec_dict["revoked"] = True
                break

        if old_rec is None:
            return None

        self._save(records)
        return self.create(name=old_rec.name, scopes=old_rec.scopes)

    def list_keys(self) -> list[APIKeyRecord]:
        """List all API keys (no secrets exposed)."""
        records = self._load()
        return [APIKeyRecord(**r) for r in records]

    def get(self, key_id: str) -> APIKeyRecord | None:
        """Get a specific API key record by ID."""
        records = self._load()
        for rec_dict in records:
            if rec_dict["id"] == key_id:
                return APIKeyRecord(**rec_dict)
        return None


# Singleton
_manager: APIKeyManager | None = None


def get_api_key_manager() -> APIKeyManager:
    global _manager
    if _manager is None:
        _manager = APIKeyManager()
    return _manager


def reset_api_key_manager() -> None:
    """Reset singleton (for testing)."""
    global _manager
    _manager = None
