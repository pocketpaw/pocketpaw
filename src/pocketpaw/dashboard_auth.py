"""Authentication middleware and token management for PocketPaw dashboard.

Extracted from dashboard.py — contains:
- ``_is_genuine_localhost()`` — checks for genuine localhost (not tunneled proxy)
- ``verify_token()`` — standalone token verification
- ``auth_middleware()`` — HTTP middleware (registered by dashboard.py)
- ``auth_router`` — APIRouter with session token, cookie login/logout, QR code,
  and token regeneration endpoints
"""

import hmac
import io
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from pocketpaw.config import Settings, get_access_token, regenerate_token
from pocketpaw.dashboard_state import _LOCALHOST_ADDRS, _PROXY_HEADERS
from pocketpaw.security.rate_limiter import api_limiter, auth_limiter
from pocketpaw.security.session_tokens import create_session_token, verify_session_token
from pocketpaw.tunnel import get_tunnel_manager

logger = logging.getLogger(__name__)

auth_router = APIRouter()


def _audit_auth_event(
    action: str,
    request: Request | None = None,
    status: str = "success",
) -> None:
    """Log an authentication event to the audit trail."""
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        severity = AuditSeverity.ALERT if status == "block" else AuditSeverity.INFO
        client_ip = ""
        if request:
            client_ip = request.client.host if request.client else "unknown"

        get_audit_logger().log(
            AuditEvent.create(
                severity=severity,
                actor="dashboard_user",
                action=action,
                target="auth",
                status=status,
                client_ip=client_ip,
            )
        )
    except Exception:
        pass  # Don't let audit failure break auth flow


# ---------------------------------------------------------------------------
# Localhost detection
# ---------------------------------------------------------------------------


def _is_genuine_localhost(request_or_ws) -> bool:
    """Check if request originates from genuine localhost (not forwarded by any proxy).

    Proxy headers (``Cf-Connecting-Ip``, ``X-Forwarded-For``) are **always**
    inspected — regardless of whether a Cloudflare tunnel is active — because
    any reverse proxy (nginx, Caddy, ngrok, cloudflared, …) can forward these
    headers.  A remote client that spoofs ``X-Forwarded-For: 127.0.0.1`` must
    not be granted the localhost bypass (OWASP A01 — Broken Access Control).

    The ``localhost_auth_bypass`` setting (default True) controls whether genuine
    localhost connections skip auth.  Set to False to require tokens everywhere.
    """
    settings = Settings.load()
    if not settings.localhost_auth_bypass:
        return False

    client_host = request_or_ws.client.host if request_or_ws.client else None
    if client_host not in _LOCALHOST_ADDRS:
        return False

    # Always check for proxy headers — regardless of whether a Cloudflare tunnel
    # is active.  Any reverse proxy (nginx, Caddy, ngrok, cloudflared, …) that
    # forwards requests will inject these headers.  A remote client that sets
    # X-Forwarded-For: 127.0.0.1 must NOT be granted the localhost bypass even
    # when the tunnel manager reports inactive (OWASP A01 — Broken Access Control,
    # see issue #871).
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
# HTTP auth middleware (registered by dashboard.py via app.add_middleware)
# ---------------------------------------------------------------------------


class AuthMiddleware:
    """Pure ASGI middleware — explicitly passes WebSocket through.

    Using a raw ASGI class instead of ``BaseHTTPMiddleware`` avoids known
    issues with Starlette's ``@app.middleware("http")`` blocking WebSocket
    connections in certain middleware stack configurations.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # WebSocket / lifespan — pass through immediately
            await self.app(scope, receive, send)
            return
        # HTTP — run the auth dispatch
        request = Request(scope, receive, send)
        rejection = await _auth_dispatch(request)
        if rejection is not None:
            await rejection(scope, receive, send)
            return
        # Allowed — inject rate-limit headers into the downstream response
        rl_headers = getattr(request.state, "rate_limit_headers", None)
        if rl_headers:

            async def send_with_headers(message):
                if message.get("type") == "http.response.start":
                    headers = list(message.get("headers", []))
                    for k, v in rl_headers.items():
                        headers.append((k.lower().encode(), v.encode()))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, send_with_headers)
        else:
            await self.app(scope, receive, send)


async def _auth_dispatch(request: Request) -> Response | None:
    """Core HTTP auth logic.  Return a Response to reject, or None to allow through."""
    # CORS preflight — always let OPTIONS through so CORSMiddleware can respond.
    if request.method == "OPTIONS":
        return None

    path = request.url.path
    client_ip = request.client.host if request.client else "unknown"

    # Rate-limit authentication endpoints BEFORE exempt-path processing.
    # Login and QR endpoints are intentionally exempt from token auth (the user
    # does not yet have a token), but they MUST still be rate-limited to prevent
    # unlimited brute-force / token-enumeration attacks (OWASP A07).
    _AUTH_RATE_LIMITED_PREFIXES = (
        "/api/auth/login",
        "/api/v1/auth/login",
        "/api/qr",
        "/api/v1/qr",
    )
    if any(path.startswith(p) for p in _AUTH_RATE_LIMITED_PREFIXES):
        rl_info = auth_limiter.check(client_ip)
        if not rl_info.allowed:
            _audit_auth_event("brute_force_blocked", request, status="block")
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers=rl_info.headers(),
            )
        request.state.rate_limit_headers = rl_info.headers()

    # Exempt routes — return None to let the request through
    exempt_paths = [
        "/static",
        "/favicon.ico",
        "/ws",  # WebSocket handles its own auth in dashboard_ws.py
        "/v1/ws",  # v1 WebSocket (short path) — same handler, same auth
        "/api/v1/ws",  # v1 WebSocket — same handler, same auth
        "/api/auth/login",
        "/api/v1/auth/login",
        "/api/v1/docs",
        "/api/v1/redoc",
        "/api/v1/openapi.json",
        "/webhook/whatsapp",
        "/webhook/inbound",
        "/api/whatsapp/qr",
        "/api/v1/whatsapp/qr",
        "/oauth/callback",
        "/api/mcp/oauth/callback",
        "/api/v1/mcp/oauth/callback",
        "/api/v1/oauth/authorize",
        "/api/v1/oauth/token",
    ]

    for exempt in exempt_paths:
        if path.startswith(exempt):
            return None  # allow through

    # Rate limiting — pick tier based on path
    client_ip = request.client.host if request.client else "unknown"
    is_auth_path = request.url.path == "/api/auth/session"
    limiter = auth_limiter if is_auth_path else api_limiter
    rl_info = limiter.check(client_ip)
    if not rl_info.allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests"},
            headers=rl_info.headers(),
        )
    # Stash rate limit info to add response headers later
    request.state.rate_limit_headers = rl_info.headers()

    # Check for token in query or header
    token = request.query_params.get("token")
    auth_header = request.headers.get("Authorization")
    current_token = get_access_token()

    is_valid = False

    # 1. Check Query Param (master token or session token)
    if token:
        if hmac.compare_digest(token, current_token):
            is_valid = True
        elif ":" in token and verify_session_token(token, current_token):
            is_valid = True

    # 2. Check Header
    elif auth_header:
        bearer_value = (
            auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
        )
        if hmac.compare_digest(bearer_value, current_token):
            is_valid = True
        elif ":" in bearer_value and verify_session_token(bearer_value, current_token):
            is_valid = True

    # 3. Check HTTP-only session cookie
    if not is_valid:
        cookie_token = request.cookies.get("pocketpaw_session")
        if cookie_token:
            if hmac.compare_digest(cookie_token, current_token):
                is_valid = True
            elif ":" in cookie_token and verify_session_token(cookie_token, current_token):
                is_valid = True

    # 4. Check API key (pp_* prefix)
    if not is_valid:
        api_key_value = None
        if token and token.startswith("pp_"):
            api_key_value = token
        elif auth_header and "pp_" in auth_header:
            api_key_value = (
                auth_header.removeprefix("Bearer ").strip()
                if auth_header.startswith("Bearer ")
                else ""
            )
        if api_key_value and api_key_value.startswith("pp_"):
            try:
                from pocketpaw.api.api_keys import get_api_key_manager
                from pocketpaw.security.rate_limiter import get_api_key_limiter

                mgr = get_api_key_manager()
                record = mgr.verify(api_key_value)
                if record:
                    # Per-API-key rate limit
                    key_rl = get_api_key_limiter().check(f"apikey:{record.id}")
                    if not key_rl.allowed:
                        return JSONResponse(
                            status_code=429,
                            content={"detail": "API key rate limit exceeded"},
                            headers=key_rl.headers(),
                        )
                    request.state.rate_limit_headers = key_rl.headers()
                    is_valid = True
                    request.state.api_key = record
            except Exception:
                logger.warning("API key validation raised an unexpected error", exc_info=True)

    # 5. Check OAuth2 access token (ppat_* prefix)
    if not is_valid:
        oauth_value = None
        if token and token.startswith("ppat_"):
            oauth_value = token
        elif auth_header:
            bearer = (
                auth_header.removeprefix("Bearer ").strip()
                if auth_header.startswith("Bearer ")
                else ""
            )
            if bearer.startswith("ppat_"):
                oauth_value = bearer
        if oauth_value:
            try:
                from pocketpaw.api.oauth2.server import get_oauth_server

                server = get_oauth_server()
                oauth_token = server.verify_access_token(oauth_value)
                if oauth_token:
                    is_valid = True
                    request.state.oauth_token = oauth_token
            except Exception:
                logger.warning("OAuth2 token validation raised an unexpected error", exc_info=True)

    # 6. Allow genuine localhost (not tunneled proxies)
    if not is_valid and _is_genuine_localhost(request):
        is_valid = True

    # Allow frontend assets (/, /static/*) through for SPA bootstrap.
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        return None  # allow through

    # Require auth for ALL remaining paths — not only /api* and /ws*.
    # Previously only API/WS paths were gated here, meaning any non-exempt
    # path that didn't start with /api or /ws (e.g. /internal/*, /v1/agents)
    # would silently fall through unauthenticated.
    if not is_valid:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    return None  # allow through


# Backward-compat alias (was previously registered via app.middleware("http"))
auth_middleware = _auth_dispatch


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

    Expects JSON body ``{"token": "..."}`` with the master access token,
    an OAuth2 access token (``ppat_*``), or an API key (``pp_*``).
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

    is_valid = hmac.compare_digest(submitted, master)
    # Accept OAuth2 access tokens (ppat_*)
    if not is_valid and submitted.startswith("ppat_"):
        try:
            from pocketpaw.api.oauth2.server import get_oauth_server

            if get_oauth_server().verify_access_token(submitted) is not None:
                is_valid = True
        except Exception:
            logger.warning("OAuth2 token verification error during login", exc_info=True)
    # Accept API keys (pp_*)
    if not is_valid and submitted.startswith("pp_") and not submitted.startswith("ppat_"):
        try:
            from pocketpaw.api.api_keys import get_api_key_manager

            if get_api_key_manager().verify(submitted) is not None:
                is_valid = True
        except Exception:
            logger.warning("API key verification error during login", exc_info=True)

    if not is_valid:
        _audit_auth_event("login_failed", request, status="block")
        return JSONResponse(status_code=401, content={"detail": "Invalid access token"})

    settings = Settings.load()
    session_token = create_session_token(master, ttl_hours=settings.session_token_ttl_hours)
    max_age = settings.session_token_ttl_hours * 3600

    _audit_auth_event("login_success", request, status="success")

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


@auth_router.post("/api/auth/logout")
async def cookie_logout(request: Request):
    """Clear the session cookie."""
    _audit_auth_event("logout", request, status="success")
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key="pocketpaw_session", path="/")
    return response


# ---------------------------------------------------------------------------
# QR Code & Token API
# ---------------------------------------------------------------------------


@auth_router.get("/api/qr")
async def get_qr_code(request: Request):
    """Generate QR login code.

    Requires authentication — the caller must already have a valid session.
    Generates a short-lived (60 s) one-time pairing token embedded in the QR URL.
    """
    import qrcode

    # Logic: If tunnel is active, use tunnel URL. Else local IP.
    host = request.headers.get("host")

    # Check for ACTIVE tunnel first to prioritize it
    tunnel = get_tunnel_manager()
    status = tunnel.get_status()

    # Short-lived pairing token (60 seconds) — scoped to the QR pairing flow
    # so a leaked QR code cannot grant long-lived access.
    qr_token = create_session_token(get_access_token(), ttl_hours=0, ttl_seconds=60)

    if status.get("active") and status.get("url"):
        login_url = f"{status['url']}/?token={qr_token}"
    else:
        # Fallback to current request host (localhost or network IP)
        protocol = "https" if "trycloudflare" in str(host) else "http"
        login_url = f"{protocol}://{host}/?token={qr_token}"

    _audit_auth_event("qr_code_generated", request, status="success")

    img = qrcode.make(login_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return StreamingResponse(buf, media_type="image/png")


@auth_router.post("/api/token/regenerate")
async def regenerate_access_token(request: Request):
    """Regenerate access token (invalidates old sessions)."""
    # This endpoint implies you are already authorized (middleware checks it)
    new_token = regenerate_token()
    _audit_auth_event("token_regenerated", request, status="success")
    return {"token": new_token}
