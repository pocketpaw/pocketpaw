# Token Store — file-based OAuth token persistence at ~/.pocketclaw/oauth/.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pocketclaw.config import get_config_dir

logger = logging.getLogger(__name__)


@dataclass
class OAuthTokens:
    """OAuth 2.0 token set for a service."""

    service: str
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: float | None = None  # Unix timestamp
    scopes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def _get_oauth_dir() -> Path:
    """Get/create the OAuth token directory."""
    d = get_config_dir() / "oauth"
    d.mkdir(exist_ok=True)
    return d


class TokenStore:
    """File-based token store at ~/.pocketclaw/oauth/{service}.json.

    Files are chmod 0600 (owner-only read/write).
    """

    def save(self, tokens: OAuthTokens) -> None:
        """Save tokens for a service."""
        path = _get_oauth_dir() / f"{tokens.service}.json"
        data = asdict(tokens)
        path.write_text(json.dumps(data, indent=2))
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        logger.info("Saved OAuth tokens for %s", tokens.service)

    def load(self, service: str) -> OAuthTokens | None:
        """Load tokens for a service. Returns None if not found."""
        path = _get_oauth_dir() / f"{service}.json"
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text())
            return OAuthTokens(**data)
        except Exception as e:
            logger.warning("Failed to load tokens for %s: %s", service, e)
            return None

    def delete(self, service: str) -> bool:
        """Delete tokens for a service. Returns True if deleted."""
        path = _get_oauth_dir() / f"{service}.json"
        if path.exists():
            path.unlink()
            logger.info("Deleted OAuth tokens for %s", service)
            return True
        return False

    def list_services(self) -> list[str]:
        """List all services with stored tokens."""
        oauth_dir = _get_oauth_dir()
        return [f.stem for f in oauth_dir.glob("*.json")]
