# Auth router — session exchange, login, logout, QR, token regen.
# Created: 2026-02-20
#
# Extracted from dashboard.py auth endpoints.

from __future__ import annotations

import io
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from pocketpaw.api.v1.schemas.auth import (
    SessionTokenResponse,
    TokenRegenerateResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Auth"])


@router.post("/auth/session", response_model=SessionTokenResponse)
async def exchange_session_token(request: Request):
    """Exchange a master access token for a time-limited session token."""
    from pocketpaw.config import Settings, get_access_token
    from pocketpaw.security.session_tokens import create_session_token

    auth_header = request.headers.get("Authorization", "")
    bearer = (
        auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
    )
    master = get_access_token()
    if bearer != master:
        raise HTTPException(status_code=401, detail="Invalid master token")

    settings = Settings.load()
    session_token = create_session_token(master, ttl_hours=settings.session_token_ttl_hours)
    return SessionTokenResponse(
        session_token=session_token,
        expires_in_hours=settings.session_token_ttl_hours,
    )


@router.post("/auth/login")
async def cookie_login(request: Request):
    """Validate access token and set an HTTP-only session cookie.

    Accepts master access token, OAuth2 token (ppat_*), or API key (pp_*).
    """
    from pocketpaw.config import Settings, get_access_token
    from pocketpaw.security.session_tokens import create_session_token

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    submitted = body.get("token", "").strip()
    master = get_access_token()

    is_valid = submitted == master
    # Accept OAuth2 access tokens (ppat_*)
    if not is_valid and submitted.startswith("ppat_"):
        try:
            from pocketpaw.api.oauth2.server import get_oauth_server

            if get_oauth_server().verify_access_token(submitted) is not None:
                is_valid = True
        except Exception:
            pass
    # Accept API keys (pp_*)
    if not is_valid and submitted.startswith("pp_") and not submitted.startswith("ppat_"):
        try:
            from pocketpaw.api.api_keys import get_api_key_manager

            if get_api_key_manager().verify(submitted) is not None:
                is_valid = True
        except Exception:
            pass

    if not is_valid:
        raise HTTPException(status_code=401, detail="Invalid access token")

    settings = Settings.load()
    session_token = create_session_token(master, ttl_hours=settings.session_token_ttl_hours)
    max_age = settings.session_token_ttl_hours * 3600

    response = JSONResponse(content={"ok": True})
    response.set_cookie(
        key="pocketpaw_session",
        value=session_token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=max_age,
    )
    return response


@router.post("/auth/logout")
async def cookie_logout():
    """Clear the session cookie."""
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key="pocketpaw_session", path="/")
    return response


@router.get("/qr")
async def get_qr_code(request: Request):
    """Generate QR login code."""
    import qrcode

    from pocketpaw.config import get_access_token
    from pocketpaw.security.session_tokens import create_session_token
    from pocketpaw.tunnel import get_tunnel_manager

    host = request.headers.get("host")

    tunnel = get_tunnel_manager()
    status = tunnel.get_status()

    qr_token = create_session_token(get_access_token(), ttl_hours=1)

    if status.get("active") and status.get("url"):
        login_url = f"{status['url']}/?token={qr_token}"
    else:
        protocol = "https" if "trycloudflare" in str(host) else "http"
        login_url = f"{protocol}://{host}/?token={qr_token}"

    img = qrcode.make(login_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return StreamingResponse(buf, media_type="image/png")


@router.post("/token/regenerate", response_model=TokenRegenerateResponse)
async def regenerate_access_token():
    """Regenerate access token (invalidates old sessions)."""
    from pocketpaw.config import regenerate_token

    new_token = regenerate_token()
    return TokenRegenerateResponse(token=new_token)
