"""Authentication middleware and token management for PocketPaw dashboard.

Extracted from dashboard.py — contains:
- ``_is_genuine_localhost()`` — checks for genuine localhost (not tunneled proxy)
- ``verify_token()`` — standalone token verification
- ``auth_middleware()`` — HTTP middleware (registered by dashboard.py)
- ``auth_router`` — APIRouter with session token, cookie login/logout, QR code,
  and token regeneration endpoints
"""

import io
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from pocketpaw.config import Settings, get_access_token, regenerate_token
from pocketpaw.dashboard_state import _LOCALHOST_ADDRS, _PROXY_HEADERS
from pocketpaw.security.rate_limiter import api_limiter, auth_limiter
from pocketpaw.security.session_tokens import create_session_token, verify_session_token
from pocketpaw.tunnel import get_tunnel_manager

logger = logging.getLogger(__name__)

auth_router = APIRouter()


# ---------------------------------------------------------------------------
# Localhost detection
# ---------------------------------------------------------------------------


def _is_genuine_localhost(request_or_ws) -> bool:
    """Check if request originates from genuine localhost (not a tunneled proxy).

    When a Cloudflare tunnel is active, requests arrive from cloudflared running
    on localhost — but they carry proxy headers (Cf-Connecting-Ip / X-Forwarded-For).
    Those are NOT genuine localhost and must authenticate.

    The ``localhost_auth_bypass`` setting (default True) controls whether genuine
    localhost connections skip auth.  Set to False to require tokens everywhere.
    """
    settings = Settings.load()
    if not settings.localhost_auth_bypass:
        return False

    client_host = request_or_ws.client.host if request_or_ws.client else None
    if client_host not in _LOCALHOST_ADDRS:
        return False

    # If the tunnel is active, check for proxy headers indicating the request
    # was forwarded by cloudflared (not a genuine local browser).
    tunnel = get_tunnel_manager()
    if tunnel.get_status()["active"]:
        headers = request_or_ws.headers
        for hdr in _PROXY_HEADERS:
            if headers.get(hdr):
                return False

    return True


# ---------------------------------------------------------------------------
# Standalone token verifier (used by some REST endpoints)
# ---------------------------------------------------------------------------


async def verify_token(
    request: Request,
    token: str | None = Query(None),
):
    """
    Verify access token from query param or Authorization header.
    """
    from fastapi import HTTPException

    # SKIP AUTH for static files and health checks (if any)
    if request.url.path.startswith("/static") or request.url.path == "/favicon.ico":
        return True

    # Check query param
    current_token = get_access_token()

    if token == current_token:
        return True

    # Check header
    auth_header = request.headers.get("Authorization")
    if auth_header:
        if auth_header == f"Bearer {current_token}":
            return True

    # Allow genuine localhost
    if _is_genuine_localhost(request):
        return True

    raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# HTTP auth middleware (registered by dashboard.py via app.middleware)
# ---------------------------------------------------------------------------


async def auth_middleware(request: Request, call_next):
    # Exempt routes
    exempt_paths = [
        "/static",
        "/favicon.ico",
        "/api/qr",
        "/api/auth/login",
        "/webhook/whatsapp",
        "/webhook/inbound",
        "/api/whatsapp/qr",
        "/oauth/callback",
        "/api/mcp/oauth/callback",
    ]

    for path in exempt_paths:
        if request.url.path.startswith(path):
            return await call_next(request)

    # Rate limiting — pick tier based on path
    client_ip = request.client.host if request.client else "unknown"
    is_auth_path = request.url.path in ("/api/auth/session", "/api/qr")
    limiter = auth_limiter if is_auth_path else api_limiter
    if not limiter.allow(client_ip):
        return JSONResponse(status_code=429, content={"detail": "Too many requests"})

    # Check for token in query or header
    token = request.query_params.get("token")
    auth_header = request.headers.get("Authorization")
    current_token = get_access_token()

    is_valid = False

    # 1. Check Query Param (master token or session token)
    if token:
        if token == current_token:
            is_valid = True
        elif ":" in token and verify_session_token(token, current_token):
            is_valid = True

    # 2. Check Header
    elif auth_header:
        bearer_value = (
            auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
        )
        if bearer_value == current_token:
            is_valid = True
        elif ":" in bearer_value and verify_session_token(bearer_value, current_token):
            is_valid = True

    # 3. Check HTTP-only session cookie
    if not is_valid:
        cookie_token = request.cookies.get("pocketpaw_session")
        if cookie_token:
            if cookie_token == current_token:
                is_valid = True
            elif ":" in cookie_token and verify_session_token(cookie_token, current_token):
                is_valid = True

    # 4. Allow genuine localhost (not tunneled proxies)
    if not is_valid and _is_genuine_localhost(request):
        is_valid = True

    # Allow frontend assets (/, /static/*) through for SPA bootstrap.
    # Only match explicit static asset paths — never suffix-match, as that
    # would let crafted URLs like /api/secrets/steal.js bypass auth.
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        return await call_next(request)

    # API Protection
    if request.url.path.startswith("/api") or request.url.path.startswith("/ws"):
        if not is_valid:
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    response = await call_next(request)
    return response


# ---------------------------------------------------------------------------
# Session Token Exchange
# ---------------------------------------------------------------------------


@auth_router.post("/api/auth/session")
async def exchange_session_token(request: Request):
    """Exchange a master access token for a time-limited session token.

    The client sends the master token in the Authorization header;
    a short-lived HMAC session token is returned.
    """
    auth_header = request.headers.get("Authorization", "")
    bearer = (
        auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
    )
    master = get_access_token()
    if bearer != master:
        return JSONResponse(status_code=401, content={"detail": "Invalid master token"})

    settings = Settings.load()
    session_token = create_session_token(master, ttl_hours=settings.session_token_ttl_hours)
    return {"session_token": session_token, "expires_in_hours": settings.session_token_ttl_hours}


# ---------------------------------------------------------------------------
# Cookie-Based Login
# ---------------------------------------------------------------------------


@auth_router.post("/api/auth/login")
async def cookie_login(request: Request):
    """Validate access token and set an HTTP-only session cookie.

    Expects JSON body ``{"token": "..."}`` with the master access token.
    Returns an HMAC session token in an HTTP-only cookie so the browser
    sends it automatically on all subsequent requests (including WebSocket
    handshakes). This is more secure than localStorage because JavaScript
    cannot read the cookie value.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    submitted = body.get("token", "").strip()
    master = get_access_token()

    if submitted != master:
        return JSONResponse(status_code=401, content={"detail": "Invalid access token"})

    settings = Settings.load()
    session_token = create_session_token(master, ttl_hours=settings.session_token_ttl_hours)
    max_age = settings.session_token_ttl_hours * 3600

    response = JSONResponse(content={"ok": True})
    response.set_cookie(
        key="pocketpaw_session",
        value=session_token,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=max_age,
    )
    return response


@auth_router.post("/api/auth/logout")
async def cookie_logout():
    """Clear the session cookie."""
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key="pocketpaw_session", path="/")
    return response


# ---------------------------------------------------------------------------
# QR Code & Token API
# ---------------------------------------------------------------------------


@auth_router.get("/api/qr")
async def get_qr_code(request: Request):
    """Generate QR login code."""
    import qrcode

    # Logic: If tunnel is active, use tunnel URL. Else local IP.
    host = request.headers.get("host")

    # Check for ACTIVE tunnel first to prioritize it
    tunnel = get_tunnel_manager()
    status = tunnel.get_status()

    # Use a short-lived session token instead of the master token
    # to limit exposure in browser history, screenshots, and logs.
    qr_token = create_session_token(get_access_token(), ttl_hours=1)

    if status.get("active") and status.get("url"):
        login_url = f"{status['url']}/?token={qr_token}"
    else:
        # Fallback to current request host (localhost or network IP)
        protocol = "https" if "trycloudflare" in str(host) else "http"
        login_url = f"{protocol}://{host}/?token={qr_token}"

    img = qrcode.make(login_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return StreamingResponse(buf, media_type="image/png")


@auth_router.post("/api/token/regenerate")
async def regenerate_access_token():
    """Regenerate access token (invalidates old sessions)."""
    # This endpoint implies you are already authorized (middleware checks it)
    new_token = regenerate_token()
    return {"token": new_token}
