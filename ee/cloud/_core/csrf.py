"""CSRF middleware + token-mint endpoint for cookie-based auth.

Created: 2026-05-17 (security #1117 P1) — Hardening the web auth chain
    so an HttpOnly ``paw_auth`` cookie can carry the JWT without exposing
    the surface area of a CSRF attack. Bearer-authenticated callers (the
    Tauri client, automation scripts, MCP tools) skip this check entirely
    because browsers never auto-attach an ``Authorization`` header.

How it fits together:

* On first state-changing request the web client calls ``GET /auth/csrf``
  and receives ``{csrf_token: "<random>"}`` plus a non-HttpOnly
  ``paw_csrf`` cookie that holds the same token. The client caches the
  token in memory and re-attaches it as ``X-CSRF-Token`` on every
  subsequent POST / PUT / PATCH / DELETE.
* The middleware below sees the matching header + cookie and lets the
  request through. If the header is missing, mismatched, or the cookie
  is gone (cleared by logout) the middleware short-circuits with a 403
  carrying ``{detail: "csrf_invalid"}`` so the client can refresh.
* Bearer requests are detected by the ``Authorization`` header; they
  bypass the check completely.
* GET/HEAD/OPTIONS are always exempt — they should be side-effect free.
* The bootstrap endpoints (login, logout, csrf, health) are exempt by
  path so the client can establish a session before any token exists.

The middleware deliberately doesn't try to enforce CSRF on requests with
no auth at all. Those routes either return 401 (auth required) or are
intentionally public; double-protecting them just adds an opaque 403 to
the error space.
"""

from __future__ import annotations

import logging
import secrets
from typing import Final

from fastapi import APIRouter, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Cookie + header names — kept module-level so tests can introspect.
CSRF_COOKIE_NAME: Final = "paw_csrf"
CSRF_HEADER_NAME: Final = "X-CSRF-Token"
AUTH_COOKIE_NAME: Final = "paw_auth"

# Verbs the browser can be tricked into firing cross-origin without a
# preflight (or that mutate state). The middleware checks CSRF on these
# and lets the rest through unmodified.
_PROTECTED_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Path prefixes that bypass CSRF entirely. ``/auth/login`` and
# ``/auth/logout`` are the bootstrap endpoints — login mints the auth
# cookie in the same response, so requiring a CSRF token to call login
# would be a chicken-and-egg. ``/auth/csrf`` itself is GET-only but
# listed for clarity. ``/health`` is a liveness probe.
_EXEMPT_PATH_PREFIXES: Final = (
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/bearer/login",
    "/api/v1/auth/bearer/logout",
    "/api/v1/auth/csrf",
    "/api/v1/auth/register",
    "/health",
)


def _generate_token() -> str:
    """Return a URL-safe random token suitable for double-submit CSRF."""

    return secrets.token_urlsafe(32)


def _path_is_exempt(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _EXEMPT_PATH_PREFIXES)


def _request_uses_bearer_auth(request: Request) -> bool:
    """Bearer tokens are not auto-sent by browsers, so CSRF doesn't apply.

    We treat any ``Authorization: Bearer ...`` header as bearer auth,
    regardless of whether the token validates downstream — the goal here
    is to decide whether the CSRF threat model applies, not whether the
    request is actually authenticated.
    """

    auth_header = request.headers.get("authorization", "")
    return auth_header.lower().startswith("bearer ")


def _request_uses_cookie_auth(request: Request) -> bool:
    return AUTH_COOKIE_NAME in request.cookies


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit CSRF for cookie-authenticated state-changing requests.

    The check is intentionally narrow: only fires when the request both
    uses cookie auth AND targets a non-exempt mutating verb. That keeps
    the existing Bearer-based clients (Tauri, MCP, scripts) on their
    current code path with zero changes.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        method = request.method.upper()

        # Safe methods: never check. Same for the bootstrap path list.
        if method not in _PROTECTED_METHODS or _path_is_exempt(request.url.path):
            response = await call_next(request)
            self._maybe_clear_csrf_on_logout(request, response)
            return response

        # Bearer caller — browsers won't auto-attach, so no CSRF surface.
        if _request_uses_bearer_auth(request):
            response = await call_next(request)
            self._maybe_clear_csrf_on_logout(request, response)
            return response

        # No cookie auth either? Let the route's own auth dep return 401
        # rather than masking it with a confusing 403.
        if not _request_uses_cookie_auth(request):
            return await call_next(request)

        cookie_value = request.cookies.get(CSRF_COOKIE_NAME, "")
        header_value = request.headers.get(CSRF_HEADER_NAME, "")

        # ``secrets.compare_digest`` so we don't leak length / prefix info
        # via timing. Both must be present and equal — empty string can't
        # match because we mint non-empty tokens.
        if not cookie_value or not header_value:
            logger.debug(
                "csrf reject: missing token (cookie=%s header=%s) path=%s",
                bool(cookie_value),
                bool(header_value),
                request.url.path,
            )
            return JSONResponse({"detail": "csrf_invalid"}, status_code=403)

        if not secrets.compare_digest(cookie_value, header_value):
            logger.debug("csrf reject: mismatch path=%s", request.url.path)
            return JSONResponse({"detail": "csrf_invalid"}, status_code=403)

        response = await call_next(request)
        self._maybe_clear_csrf_on_logout(request, response)
        return response

    def _maybe_clear_csrf_on_logout(self, request: Request, response) -> None:
        """If this request hit a logout endpoint, expire the paw_csrf
        cookie alongside the auth cookie fastapi-users just cleared.

        Without this, paw_csrf lives for its 7-day max-age after logout —
        JS can still read it (it's intentionally NOT HttpOnly) and submit
        it on the next login. Clearing here keeps the two cookies'
        lifecycles paired without forking the fastapi-users logout route.
        """
        path = request.url.path
        is_logout = path in {
            "/api/v1/auth/logout",
            "/api/v1/auth/cookie/logout",
            "/api/v1/auth/bearer/logout",
        }
        if not is_logout:
            return
        # Only clear on a successful logout — leave the cookie alone if
        # fastapi-users rejected the request.
        if 200 <= response.status_code < 300:
            response.delete_cookie(
                CSRF_COOKIE_NAME, path="/", samesite="lax", secure=False
            )


# ---------------------------------------------------------------------------
# Router — exposes GET /auth/csrf for clients to mint a token
# ---------------------------------------------------------------------------

csrf_router = APIRouter(tags=["Auth"])


@csrf_router.get("/auth/csrf")
async def get_csrf_token(request: Request, response: Response) -> dict[str, str]:
    """Mint (or rotate) a CSRF token and set the double-submit cookie.

    Returns the token in the body so the client can cache it in memory
    (where it lives next to the in-flight request, not in
    persistent storage). Also writes the token to the ``paw_csrf``
    cookie, which is intentionally NOT HttpOnly so JS can read it — the
    secret is the *match* between header and cookie, not either value
    alone.

    Idempotent: calling repeatedly just rotates. Safe to call before the
    user is authenticated; the token is per-browser, not per-user, so
    the same token survives login.
    """

    # Reuse the existing cookie if the caller already has one. Rotating
    # on every fetch would invalidate any in-flight request that grabbed
    # the previous value from /auth/csrf milliseconds earlier.
    token = request.cookies.get(CSRF_COOKIE_NAME) or _generate_token()

    # cookie_secure mirrors the auth cookie's posture so dev / prod
    # stays consistent. Importing inside the function keeps this module
    # free of an auth.core import cycle.
    from ee.cloud.auth.core import _COOKIE_SECURE

    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        max_age=60 * 60 * 24 * 7,  # 7 days — match JWT lifetime
        secure=_COOKIE_SECURE,
        httponly=False,  # JS must read this to echo it back as a header
        samesite="lax",
        path="/",
    )
    return {"csrf_token": token}


__all__ = [
    "AUTH_COOKIE_NAME",
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "CSRFMiddleware",
    "csrf_router",
]
