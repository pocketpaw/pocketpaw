# OAuth Integration routes — authorize + callback for Google, Spotify, etc.

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

# Map OAuth service names to connector registry names.
# OAuth uses "google_drive" but connectors use "drive" — this bridges the two.
SERVICE_TO_CONNECTOR: dict[str, str] = {
    "google_gmail": "gmail",
    "google_calendar": "gcalendar",
    "google_drive": "drive",
    "google_docs": "gdocs",
    "spotify": "spotify",
}

# Reverse map: connector registry name -> OAuth service name
CONNECTOR_TO_SERVICE: dict[str, str] = {v: k for k, v in SERVICE_TO_CONNECTOR.items()}


@router.get("/oauth/integrations/authorize")
async def oauth_authorize(service: str = Query("google_gmail")):
    """Start OAuth flow — redirects user to provider consent screen.

    Accepts either an OAuth service name (``google_drive``) or a connector
    name (``drive``). Connector names are resolved via ``CONNECTOR_TO_SERVICE``.
    """
    from fastapi.responses import RedirectResponse

    from pocketpaw.config import Settings

    settings = Settings.load()

    # Accept both service names (google_drive) and connector names (drive).
    resolved = CONNECTOR_TO_SERVICE.get(service, service)
    scopes = OAUTH_SCOPES.get(resolved)
    if not scopes:
        raise HTTPException(status_code=400, detail=f"Unknown service: {service}")

    if resolved == "spotify":
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
    state = f"{provider}:{resolved}"

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

        parts = state.split(":", 1)
        provider = parts[0] if parts else "google"
        service = parts[1] if len(parts) > 1 else "google_gmail"

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
        )

        return HTMLResponse(
            "<h2>Authorization Successful</h2>"
            "<p>Tokens saved. This window will close automatically.</p>"
            "<script>setTimeout(() => window.close(), 1500)</script>"
        )

    except Exception as e:
        logger.error("OAuth callback error: %s", e)
        return HTMLResponse(f"<h2>OAuth Error</h2><p>{e}</p>")
