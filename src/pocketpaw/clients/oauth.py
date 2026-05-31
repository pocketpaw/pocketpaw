# OAuth Manager — Google OAuth 2.0 auth code flow + token refresh.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

from __future__ import annotations

import base64
import logging
import time
import urllib.parse

import httpx

from pocketpaw.clients.token_store import OAuthTokens, TokenStore

logger = logging.getLogger(__name__)


# OAuth 2.0 provider configuration.
# `grant_type` key marks providers that use a non-default flow on refresh.
# Default (omitted) = 3-leg authorization_code + refresh_token grant.
PROVIDERS: dict[str, dict[str, str]] = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "revoke_url": "https://oauth2.googleapis.com/revoke",
    },
    "google_meet": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "revoke_url": "https://oauth2.googleapis.com/revoke",
    },
    "spotify": {
        "auth_url": "https://accounts.spotify.com/authorize",
        "token_url": "https://accounts.spotify.com/api/token",
        "revoke_url": "",
    },
    "zoom": {
        "auth_url": "",  # S2S has no browser flow
        "token_url": "https://zoom.us/oauth/token",
        "revoke_url": "https://zoom.us/oauth/revoke",
        "grant_type": "account_credentials",
    },
}


class OAuthManager:
    """Google OAuth 2.0 authorization code flow + token refresh.

    Supports:
    - Authorization URL generation
    - Code exchange for tokens
    - Token refresh
    - Token validation

    Extensible to other providers by adding to PROVIDERS dict.
    """

    def __init__(self, token_store: TokenStore | None = None):
        self.store = token_store or TokenStore()

    def get_auth_url(
        self,
        provider: str,
        client_id: str,
        redirect_uri: str,
        scopes: list[str],
        state: str = "",
    ) -> str:
        """Generate an OAuth authorization URL.

        Args:
            provider: Provider name (e.g. "google").
            client_id: OAuth client ID.
            redirect_uri: Redirect URI after authorization.
            scopes: List of OAuth scopes to request.
            state: Optional state parameter for CSRF protection.

        Returns:
            Authorization URL to redirect the user to.
        """
        config = PROVIDERS.get(provider)
        if not config:
            raise ValueError(f"Unknown OAuth provider: {provider}")

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",
        }
        if state:
            params["state"] = state

        return f"{config['auth_url']}?{urllib.parse.urlencode(params)}"

    async def exchange_code(
        self,
        provider: str,
        service: str,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str] | None = None,
    ) -> OAuthTokens:
        """Exchange an authorization code for access + refresh tokens.

        Args:
            provider: Provider name (e.g. "google").
            service: Service identifier for token storage (e.g. "google_gmail").
            code: Authorization code from the callback.
            client_id: OAuth client ID.
            client_secret: OAuth client secret.
            redirect_uri: Same redirect URI used in the auth request.
            scopes: Scopes that were requested (stored with tokens).

        Returns:
            OAuthTokens with access and refresh tokens.
        """
        config = PROVIDERS.get(provider)
        if not config:
            raise ValueError(f"Unknown OAuth provider: {provider}")

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                config["token_url"],
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        expires_in = data.get("expires_in", 3600)
        tokens = OAuthTokens(
            service=service,
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=time.time() + expires_in,
            scopes=scopes or [],
        )

        self.store.save(tokens)
        logger.info("OAuth tokens obtained for %s via %s", service, provider)
        return tokens

    async def exchange_account_credentials(
        self,
        provider: str,
        service: str,
        client_id: str,
        client_secret: str,
        account_id: str,
        scopes: list[str] | None = None,
    ) -> OAuthTokens:
        """Server-to-Server OAuth (account_credentials grant).

        Used by Zoom S2S OAuth apps where the app acts on behalf of an
        entire account rather than a single user. No refresh token —
        a new access token is requested whenever the current one expires.

        The account_id is persisted in tokens.extra so future "refreshes"
        can re-request without the caller having to track it.
        """
        config = PROVIDERS.get(provider)
        if not config:
            raise ValueError(f"Unknown OAuth provider: {provider}")

        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                config["token_url"],
                headers={"Authorization": f"Basic {basic}"},
                data={
                    "grant_type": "account_credentials",
                    "account_id": account_id,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        expires_in = data.get("expires_in", 3600)
        # We persist client_id + client_secret alongside account_id in
        # the token blob (chmod 0600, never in Mongo). Downstream callers
        # (the meetings adapter factory, the refresh path) read them
        # back without the caller having to track them separately.
        tokens = OAuthTokens(
            service=service,
            access_token=data["access_token"],
            refresh_token=None,
            token_type=data.get("token_type", "Bearer"),
            expires_at=time.time() + expires_in,
            scopes=scopes or [s for s in data.get("scope", "").split(" ") if s],
            extra={
                "account_id": account_id,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )

        self.store.save(tokens)
        logger.info("S2S OAuth tokens obtained for %s via %s", service, provider)
        return tokens

    async def refresh_token(
        self,
        provider: str,
        service: str,
        client_id: str,
        client_secret: str,
    ) -> OAuthTokens | None:
        """Refresh an expired access token.

        For standard 3-leg providers uses the stored refresh_token.
        For S2S (account_credentials) providers re-requests via the
        original account_id, which is read from tokens.extra.

        Returns updated OAuthTokens, or None if refresh fails.
        """
        config = PROVIDERS.get(provider)
        if not config:
            return None

        if config.get("grant_type") == "account_credentials":
            tokens = self.store.load(service)
            if not tokens:
                return None
            account_id = tokens.extra.get("account_id")
            if not account_id:
                logger.warning("S2S refresh missing account_id for %s", service)
                return None
            try:
                return await self.exchange_account_credentials(
                    provider, service, client_id, client_secret, account_id, tokens.scopes
                )
            except Exception as e:
                logger.warning("S2S token refresh failed for %s: %s", service, e)
                return None

        tokens = self.store.load(service)
        if not tokens or not tokens.refresh_token:
            return None

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    config["token_url"],
                    data={
                        "refresh_token": tokens.refresh_token,
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "refresh_token",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            expires_in = data.get("expires_in", 3600)
            tokens.access_token = data["access_token"]
            tokens.expires_at = time.time() + expires_in
            if "refresh_token" in data:
                tokens.refresh_token = data["refresh_token"]

            self.store.save(tokens)
            logger.info("Refreshed OAuth token for %s", service)
            return tokens

        except Exception as e:
            logger.warning("Token refresh failed for %s: %s", service, e)
            return None

    async def get_valid_token(
        self,
        service: str,
        client_id: str,
        client_secret: str,
        provider: str = "google",
    ) -> str | None:
        """Get a valid access token, refreshing if expired.

        Returns the access token string, or None if unavailable.
        """
        tokens = self.store.load(service)
        if not tokens:
            return None

        # Check if token is still valid (with 60s buffer)
        if tokens.expires_at and tokens.expires_at > time.time() + 60:
            return tokens.access_token

        # Try to refresh
        refreshed = await self.refresh_token(provider, service, client_id, client_secret)
        if refreshed:
            return refreshed.access_token

        return None
