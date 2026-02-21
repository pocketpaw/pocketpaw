# OAuth2 token and code storage.
# Created: 2026-02-20
#
# File-backed token persistence — tokens survive server restarts.
# Auth codes remain in-memory (short-lived, 10 min TTL).

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from pocketpaw.api.oauth2.models import AuthorizationCode, OAuthClient, OAuthToken

logger = logging.getLogger(__name__)

# Default desktop client — always registered
DEFAULT_DESKTOP_CLIENT = OAuthClient(
    client_id="pocketpaw-desktop",
    client_name="PocketPaw Desktop",
    redirect_uris=["tauri://oauth-callback", "http://localhost:1420/oauth-callback"],
    allowed_scopes=["chat", "sessions", "settings:read", "settings:write", "channels", "memory", "admin"],
)


def _default_persist_path() -> Path:
    from pocketpaw.config import get_config_dir

    return get_config_dir() / "oauth_tokens.json"


class OAuthStorage:
    """File-backed OAuth2 storage.

    Auth codes are in-memory only (ephemeral, 10 min TTL).
    Tokens are persisted to disk so refresh tokens survive restarts.
    """

    def __init__(self, persist_path: Path | None = None):
        self._clients: dict[str, OAuthClient] = {
            DEFAULT_DESKTOP_CLIENT.client_id: DEFAULT_DESKTOP_CLIENT,
        }
        self._codes: dict[str, AuthorizationCode] = {}
        self._tokens: dict[str, OAuthToken] = {}  # keyed by access_token
        self._refresh_index: dict[str, str] = {}  # refresh_token → access_token
        self._persist_path = persist_path
        self._load_tokens()

    def _get_path(self) -> Path:
        if self._persist_path is not None:
            return self._persist_path
        return _default_persist_path()

    def _load_tokens(self) -> None:
        """Load tokens from disk on startup."""
        path = self._get_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            now = datetime.now(UTC)
            for entry in data:
                token = OAuthToken(
                    access_token=entry["access_token"],
                    refresh_token=entry["refresh_token"],
                    client_id=entry["client_id"],
                    scope=entry["scope"],
                    token_type=entry.get("token_type", "Bearer"),
                    expires_at=datetime.fromisoformat(entry["expires_at"])
                    if entry.get("expires_at")
                    else None,
                    created_at=datetime.fromisoformat(entry["created_at"])
                    if entry.get("created_at")
                    else now,
                    revoked=entry.get("revoked", False),
                )
                self._tokens[token.access_token] = token
                self._refresh_index[token.refresh_token] = token.access_token
            logger.debug("Loaded %d OAuth tokens from %s", len(self._tokens), path)
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("Failed to load OAuth tokens from %s: %s", path, exc)

    def _save_tokens(self) -> None:
        """Persist tokens to disk."""
        path = self._get_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for token in self._tokens.values():
            data.append(
                {
                    "access_token": token.access_token,
                    "refresh_token": token.refresh_token,
                    "client_id": token.client_id,
                    "scope": token.scope,
                    "token_type": token.token_type,
                    "expires_at": token.expires_at.isoformat() if token.expires_at else None,
                    "created_at": token.created_at.isoformat(),
                    "revoked": token.revoked,
                }
            )
        path.write_text(json.dumps(data, indent=2))
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def get_client(self, client_id: str) -> OAuthClient | None:
        return self._clients.get(client_id)

    def store_code(self, code: AuthorizationCode) -> None:
        self._codes[code.code] = code

    def get_code(self, code: str) -> AuthorizationCode | None:
        return self._codes.get(code)

    def mark_code_used(self, code: str) -> None:
        if code in self._codes:
            self._codes[code].used = True

    def store_token(self, token: OAuthToken) -> None:
        self._tokens[token.access_token] = token
        self._refresh_index[token.refresh_token] = token.access_token
        self._save_tokens()

    def get_token(self, access_token: str) -> OAuthToken | None:
        return self._tokens.get(access_token)

    def get_token_by_refresh(self, refresh_token: str) -> OAuthToken | None:
        access_token = self._refresh_index.get(refresh_token)
        if access_token:
            return self._tokens.get(access_token)
        return None

    def revoke_token(self, access_token: str) -> bool:
        token = self._tokens.get(access_token)
        if token and not token.revoked:
            token.revoked = True
            self._save_tokens()
            return True
        return False

    def remove_refresh_token(self, refresh_token: str) -> None:
        """Remove a refresh token from the index so it cannot be reused."""
        self._refresh_index.pop(refresh_token, None)

    def revoke_by_refresh(self, refresh_token: str) -> bool:
        access_token = self._refresh_index.get(refresh_token)
        if access_token:
            return self.revoke_token(access_token)
        return False

    def cleanup_expired(self) -> None:
        """Remove expired codes and tokens."""
        now = datetime.now(UTC)
        # Codes expire after 10 minutes
        expired_codes = [
            k
            for k, v in self._codes.items()
            if (now - v.created_at).total_seconds() > 600 or v.used
        ]
        for k in expired_codes:
            del self._codes[k]

        # Tokens expire based on expires_at
        expired_tokens = [
            k for k, v in self._tokens.items() if v.expires_at and now > v.expires_at
        ]
        changed = bool(expired_tokens)
        for k in expired_tokens:
            token = self._tokens.pop(k)
            self._refresh_index.pop(token.refresh_token, None)

        if changed:
            self._save_tokens()
