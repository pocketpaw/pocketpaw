# OAuth2 data models.
# Created: 2026-02-20

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class OAuthClient:
    """Registered OAuth2 client."""

    client_id: str
    client_name: str
    redirect_uris: list[str] = field(default_factory=list)
    allowed_scopes: list[str] = field(default_factory=lambda: ["chat", "sessions"])


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
