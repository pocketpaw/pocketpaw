# OAuth Integration routes — authorize + callback for Google, Spotify, etc.
# Created: 2026-03-31
# Extracted from dashboard.py so these work in both dashboard and serve modes.
# 2026-06-08: authorize accepts an optional user_id and embeds it in the OAuth
#   ``state`` (provider:service:user_id); the callback parses it back out and
#   stores tokens in that user's bucket (VIP Onboarding Phase B). Omitting
#   user_id keeps the shared single-user flow (state stays provider:service).

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["OAuth Integrations"])

# OAuth scopes per service (canonical source)
OAUTH_SCOPES: dict[str, list[str]] = {
    "google_gmail": ["https://mail.google.com/"],
    "google_calendar": ["https://www.googleapis.com/auth/calendar"],
    "google_drive": ["https://www.googleapis.com/auth/drive"],
    "google_docs": [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive.readonly",
    ],
    "spotify": [
        "user-read-playback-state",
        "user-modify-playback-state",
        "user-read-currently-playing",
        "playlist-read-private",
        "playlist-modify-public",
        "playlist-modify-private",
    ],
}


@router.get("/oauth/integrations/authorize")
async def oauth_authorize(
    service: str = Query("google_gmail"),
    user_id: str = Query(""),
):
    """Start OAuth flow — redirects user to provider consent screen.

    ``user_id`` (optional) scopes the resulting token to a single member so
    each person connects their own account (VIP Onboarding Phase B). When
    omitted, tokens land in the shared single-user bucket.
    """
    from fastapi.responses import RedirectResponse

    from pocketpaw.config import Settings

    settings = Settings.load()

    scopes = OAUTH_SCOPES.get(service)
    if not scopes:
        raise HTTPException(status_code=400, detail=f"Unknown service: {service}")

    if service == "spotify":
        provider = "spotify"
        client_id = settings.spotify_client_id
        if not client_id:
            raise HTTPException(
                status_code=400,
                detail="Spotify Client ID not configured. Set it in Settings first.",
            )
    else:
        provider = "google"
        client_id = settings.google_oauth_client_id
        if not client_id:
            raise HTTPException(
                status_code=400,
                detail="Google OAuth Client ID not configured. Set it in Settings first.",
            )

    from pocketpaw.clients.oauth import OAuthManager

    manager = OAuthManager()
    redirect_uri = f"http://localhost:{settings.web_port}/api/v1/oauth/integrations/callback"
    # Carry an optional user_id as a third state segment so the callback can
    # store the token in that member's bucket. Stays provider:service when
    # no user_id is supplied (back-compat with the single-user flow).
    state = f"{provider}:{service}:{user_id}" if user_id else f"{provider}:{service}"

    auth_url = manager.get_auth_url(
        provider=provider,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
        state=state,
    )
    return RedirectResponse(auth_url)


@router.get("/oauth/integrations/callback")
async def oauth_callback(
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
):
    """OAuth callback — exchanges auth code for tokens."""
    from fastapi.responses import HTMLResponse

    if error:
        return HTMLResponse(f"<h2>OAuth Error</h2><p>{error}</p><p>You can close this window.</p>")

    if not code:
        return HTMLResponse("<h2>Missing authorization code</h2>")

    try:
        from pocketpaw.clients.oauth import OAuthManager
        from pocketpaw.clients.token_store import TokenStore
        from pocketpaw.config import Settings

        settings = Settings.load()
        manager = OAuthManager(TokenStore())

        # state shape: "provider:service" (legacy) or "provider:service:user_id".
        parts = state.split(":", 2)
        provider = parts[0] if parts else "google"
        service = parts[1] if len(parts) > 1 else "google_gmail"
        user_id = parts[2] if len(parts) > 2 and parts[2] else None

        redirect_uri = f"http://localhost:{settings.web_port}/api/v1/oauth/integrations/callback"
        scopes = OAUTH_SCOPES.get(service, [])

        if provider == "spotify":
            client_id = settings.spotify_client_id or ""
            client_secret = settings.spotify_client_secret or ""
        else:
            client_id = settings.google_oauth_client_id or ""
            client_secret = settings.google_oauth_client_secret or ""

        await manager.exchange_code(
            provider=provider,
            service=service,
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
            user_id=user_id,
        )

        return HTMLResponse(
            "<h2>Authorization Successful</h2>"
            "<p>Tokens saved. This window will close automatically.</p>"
            "<script>setTimeout(() => window.close(), 1500)</script>"
        )

    except Exception as e:
        logger.error("OAuth callback error: %s", e)
        return HTMLResponse(f"<h2>OAuth Error</h2><p>{e}</p>")
