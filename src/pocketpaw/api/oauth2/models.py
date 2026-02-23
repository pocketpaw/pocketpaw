# OAuth2 data models.
# Created: 2026-02-20

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse


@dataclass
class OAuthClient:
    """Registered OAuth2 client."""

    client_id: str
    client_name: str
    redirect_uris: list[str] = field(default_factory=list)
    allowed_scopes: list[str] = field(default_factory=lambda: ["chat", "sessions"])

    def matches_redirect_uri(self, uri: str) -> bool:
        """Check if a redirect URI is allowed for this client.

        Per RFC 8252 Section 7.3, native apps using loopback redirects
        (http://localhost or http://127.0.0.1) may use any port. The port
        is excluded from the comparison; paths are normalized (empty → /).
        """
        if uri in self.redirect_uris:
            return True

        parsed = urlparse(uri)
        if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"):
            path = parsed.path or "/"
            for registered in self.redirect_uris:
                rp = urlparse(registered)
                if rp.scheme == "http" and rp.hostname in ("localhost", "127.0.0.1"):
                    if path == (rp.path or "/"):
                        return True

        return False


@dataclass
class AuthorizationCode:
    """Short-lived authorization code for PKCE exchange."""

    code: str
    client_id: str
    redirect_uri: str
    scope: str
    code_challenge: str
    code_challenge_method: str  # "S256"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    used: bool = False


@dataclass
class OAuthToken:
    """OAuth2 access + refresh token pair."""

    access_token: str
    refresh_token: str
    client_id: str
    scope: str
    token_type: str = "Bearer"
    expires_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    revoked: bool = False
