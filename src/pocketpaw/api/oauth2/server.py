# OAuth2 Authorization Server with PKCE support.
# Created: 2026-02-20
#
# Implements the authorization code flow with PKCE (RFC 7636) for
# secure desktop app authentication.

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from pocketpaw.api.oauth2.models import AuthorizationCode, OAuthToken
from pocketpaw.api.oauth2.storage import OAuthStorage

logger = logging.getLogger(__name__)

# Token lifetimes
ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=30)
CODE_TTL = timedelta(minutes=10)


class AuthorizationServer:
    """OAuth2 authorization server with PKCE."""

    def __init__(self, storage: OAuthStorage | None = None):
        self.storage = storage or OAuthStorage()

    def authorize(
        self,
        client_id: str,
        redirect_uri: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
    ) -> tuple[str | None, str | None]:
        """Create an authorization code for the PKCE flow.

        Returns (code, error). If error is not None, code is None.
        """
        client = self.storage.get_client(client_id)
        if client is None:
            return None, "invalid_client"

        if redirect_uri not in client.redirect_uris:
            return None, "invalid_redirect_uri"

        if code_challenge_method != "S256":
            return None, "invalid_code_challenge_method"

        if not code_challenge:
            return None, "missing_code_challenge"

        # Validate scopes
        requested = set(scope.split()) if scope else set()
        allowed = set(client.allowed_scopes)
        if not requested.issubset(allowed):
            return None, "invalid_scope"

        code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        self.storage.store_code(auth_code)
        return code, None

    def exchange(
        self,
        code: str,
        client_id: str,
        code_verifier: str,
        redirect_uri: str = "",
    ) -> tuple[dict | None, str | None]:
        """Exchange an authorization code + verifier for tokens.

        Returns (token_dict, error).
        """
        auth_code = self.storage.get_code(code)
        if auth_code is None:
            return None, "invalid_code"

        if auth_code.used:
            return None, "code_already_used"

        # Check expiry
        now = datetime.now(UTC)
        if (now - auth_code.created_at) > CODE_TTL:
            return None, "code_expired"

        if auth_code.client_id != client_id:
            return None, "client_mismatch"

        if redirect_uri and auth_code.redirect_uri != redirect_uri:
            return None, "redirect_uri_mismatch"

        # PKCE verification: S256 = BASE64URL(SHA256(code_verifier))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if challenge != auth_code.code_challenge:
            return None, "invalid_code_verifier"

        # Mark code as used
        self.storage.mark_code_used(code)

        # Generate tokens
        access_token = f"ppat_{secrets.token_urlsafe(32)}"
        refresh_token = f"pprt_{secrets.token_urlsafe(32)}"

        token = OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            client_id=client_id,
            scope=auth_code.scope,
            expires_at=now + ACCESS_TOKEN_TTL,
        )
        self.storage.store_token(token)

        try:
            from pocketpaw.security.audit import get_audit_logger

            get_audit_logger().log_api_event(
                action="oauth_token",
                target=f"client:{client_id}",
                scope=auth_code.scope,
            )
        except Exception:
            pass

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
            "scope": auth_code.scope,
        }, None

    def refresh(self, refresh_token: str) -> tuple[dict | None, str | None]:
        """Refresh an access token using a refresh token.

        Returns (token_dict, error).
        """
        old_token = self.storage.get_token_by_refresh(refresh_token)
        if old_token is None or old_token.revoked:
            return None, "invalid_refresh_token"

        # Revoke old token and remove old refresh token from index
        self.storage.revoke_token(old_token.access_token)
        self.storage.remove_refresh_token(refresh_token)

        # Generate new token pair
        now = datetime.now(UTC)
        new_access = f"ppat_{secrets.token_urlsafe(32)}"
        new_refresh = f"pprt_{secrets.token_urlsafe(32)}"

        token = OAuthToken(
            access_token=new_access,
            refresh_token=new_refresh,
            client_id=old_token.client_id,
            scope=old_token.scope,
            expires_at=now + ACCESS_TOKEN_TTL,
        )
        self.storage.store_token(token)

        return {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
            "scope": old_token.scope,
        }, None

    def revoke(self, token: str) -> bool:
        """Revoke an access or refresh token."""
        if self.storage.revoke_token(token):
            return True
        return self.storage.revoke_by_refresh(token)

    def verify_access_token(self, access_token: str) -> OAuthToken | None:
        """Verify an access token and return the token record if valid."""
        token = self.storage.get_token(access_token)
        if token is None or token.revoked:
            return None
        if token.expires_at and datetime.now(UTC) > token.expires_at:
            return None
        return token


# Singleton
_server: AuthorizationServer | None = None


def get_oauth_server() -> AuthorizationServer:
    global _server
    if _server is None:
        _server = AuthorizationServer()
    return _server


def reset_oauth_server() -> None:
    global _server
    _server = None
