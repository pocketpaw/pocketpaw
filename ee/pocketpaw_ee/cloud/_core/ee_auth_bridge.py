"""Bridge EE JWT auth into the OSS ``AuthMiddleware``'s request state.

Runs just outside the OSS ``AuthMiddleware``. For a request carrying a
fastapi-users JWT (``paw_auth`` cookie or a Bearer that is not ``pp_``/``ppat_``)
it decodes the token, rejects revoked sessions, loads the User, and sets:

  * ``workspace_id`` / ``user_id`` — the session's tenant, for OSS routers that
    must not trust an ``X-Workspace-Id`` header.
  * ``ee_user_authenticated`` — active users only (guests included). A
    limiter-only marker: on ``/api/v1/`` the OSS middleware keys its
    ``api_limiter`` on ``user:<user_id>`` instead of the client IP. It grants
    no access and is no exemption from the limiter.
  * ``full_access`` — platform admins (``is_superuser``) ONLY. It is the OSS
    superuser bypass that skips every ``require_scope`` check, so it is never
    derived from a workspace role: a self-service workspace owner on shared
    infra must stay subject to OSS scopes (the W4b escalation fix).

Everything is best-effort: a bad, expired or revoked token just leaves state
untouched and the route's own auth decides. Cost is a local HMAC decode plus
one ``User.get()``; static/auth-flow paths and token-less requests skip it.
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
    """Mark EE-authenticated *platform admin* requests as ``full_access`` for OSS."""

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

        # Stamp the SESSION's tenant so OSS-package routers mounted under
        # /api/v1/ can scope themselves without importing pocketpaw_ee (the
        # open-core import boundary forbids it, and an import-linter contract
        # enforces that).
        #
        # This exists because src/pocketpaw/api/v1/cloud_projects.py stores
        # per-tenant data and had no way to learn the tenant, so it read one
        # out of an X-Workspace-Id header. A header is chosen by the caller, so
        # every tenant's project storage was readable and writable by anyone
        # who named the workspace. These two attributes are the supported way
        # for an OSS router to get an authenticated tenant; nothing else on
        # request.state carries one.
        #
        # Set for EVERY resolved user, not only superusers — an ordinary
        # workspace member is exactly who needs their own workspace resolved.
        request.state.workspace_id = getattr(user, "active_workspace", None)
        request.state.user_id = str(getattr(user, "id", "") or "") or None

        # Limiter-only marker: the OSS AuthMiddleware keys its api_limiter on
        # this user instead of the client IP. It is NOT an auth signal — nothing
        # grants access on it, and it never sets full_access. Inactive users
        # stay on the per-IP bucket like anonymous callers.
        if getattr(user, "is_active", False):
            request.state.ee_user_authenticated = True

        # full_access is the OSS superuser bypass — reserve it for genuine
        # platform administrators. A workspace owner/admin is NOT a superuser
        # over the OSS guards (settings/channels/budget); granting it here let
        # a self-service tenant on shared infra escalate platform-wide. They
        # remain subject to OSS require_scope like everyone else.
        if getattr(user, "is_superuser", False):
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
