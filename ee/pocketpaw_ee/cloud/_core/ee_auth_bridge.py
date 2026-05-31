"""Bridge EE JWT auth → OSS ``request.state.full_access`` for admin/owner.

OSS routes under ``src/pocketpaw/api/v1/`` (settings, channels, budget, soul,
...) gate access with ``require_scope(...)`` from ``pocketpaw.api.deps``. That
dependency accepts:

  * ``request.state.full_access`` truthy — the cookie/session/master-token
    paths in ``dashboard_auth.py`` set this for fully-trusted callers.
  * ``request.state.api_key`` with matching scopes.
  * ``request.state.oauth_token`` with matching scopes.

The EE cloud uses fastapi-users JWT (cookie ``paw_auth`` or Bearer) at the
route level. The OSS ``AuthMiddleware`` doesn't know about EE auth, so a
fully-authenticated cloud admin hitting ``/api/v1/settings`` would 403 with
``Missing required scope: settings:read or settings:write`` because nothing
sets ``full_access``.

This middleware closes that gap. On every request:

  1. Decode the JWT from ``paw_auth`` cookie or ``Authorization: Bearer``.
  2. Resolve the User and their active workspace role.
  3. If owner or admin, set ``request.state.full_access = True``.

Members + viewers stay locked out — settings + channels are platform-grade
config that shouldn't be writable by every workspace member.

Performance: the JWT decode is local (HMAC); the User lookup is one Beanie
``get()`` per request. We skip entirely for paths that already exempt from
auth (static assets, oauth callbacks) and for requests with no cookie or
bearer at all.
"""

from __future__ import annotations

import logging

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# Paths the OSS AuthMiddleware skips entirely. Don't waste a JWT decode
# on these.
_EXEMPT_PREFIXES = (
    "/static/",
    "/uploads/",
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/bearer/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/forgot-password",
    "/api/v1/auth/reset-password",
    "/api/v1/auth/request-verify-token",
    "/api/v1/auth/verify",
)


class EEAuthBridgeMiddleware(BaseHTTPMiddleware):
    """Mark EE-authenticated admin/owner requests as ``full_access`` for OSS."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path == "/" or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await call_next(request)

        # Pull the JWT from cookie first, then Authorization header. We don't
        # care which transport authenticated the caller — both are valid EE
        # auth surfaces.
        token = request.cookies.get("paw_auth")
        if not token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                bearer = auth_header.removeprefix("Bearer ").strip()
                # Skip OSS-issued API keys (pp_*) and OAuth tokens (ppat_*).
                # Those are handled by the OSS AuthMiddleware cascade with
                # their own scope semantics.
                if bearer and not bearer.startswith(("pp_", "ppat_")):
                    token = bearer

        if not token:
            return await call_next(request)

        user = await _resolve_user(token)
        if user is None:
            return await call_next(request)

        role = _active_workspace_role(user)
        if role in ("owner", "admin"):
            request.state.full_access = True

        return await call_next(request)


async def _resolve_user(token: str):  # noqa: ANN202 — Beanie Document, avoid circular import
    """Decode the JWT and load the User document. Returns None on any failure."""
    try:
        # Lazy imports — keeps middleware module light and avoids triggering
        # the EE auth chain on processes that don't mount the cloud.
        from pocketpaw_ee.cloud.auth import sessions as sessions_service
        from pocketpaw_ee.cloud.auth.core import SECRET, RevocableJWTStrategy
        from pocketpaw_ee.cloud.models.user import User

        strategy = RevocableJWTStrategy(secret=SECRET, lifetime_seconds=1)
        try:
            payload = jwt.decode(
                token,
                strategy.decode_key
                if isinstance(strategy.decode_key, str)
                else strategy.decode_key.get_secret_value(),
                audience=strategy.token_audience,
                algorithms=[strategy.algorithm],
            )
        except jwt.PyJWTError:
            return None

        jti = payload.get("jti")
        user_id = payload.get("sub")
        if not user_id:
            return None
        if jti and await sessions_service.is_revoked(user_id, jti):
            return None
        return await User.get(user_id)
    except Exception:
        # Swallow — bridge auth is best-effort. A failure here just means the
        # caller doesn't get full_access; the route's own auth still runs.
        logger.debug("EE auth bridge failed to resolve user", exc_info=True)
        return None


def _active_workspace_role(user) -> str | None:  # noqa: ANN001 — User Document
    """Return the user's role string on their currently-active workspace, or None."""
    active = getattr(user, "active_workspace", None)
    if not active:
        return None
    for membership in getattr(user, "workspaces", []) or []:
        if getattr(membership, "workspace", None) == active:
            return getattr(membership, "role", None)
    return None
