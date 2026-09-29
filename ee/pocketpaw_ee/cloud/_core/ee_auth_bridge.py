"""Bridge EE JWT auth into the OSS ``AuthMiddleware``, and own the request scope.

Pure ASGI. Runs just outside the OSS ``AuthMiddleware``; websockets and
lifespan pass through untouched. For an HTTP request carrying a fastapi-users
JWT (``paw_auth`` cookie, else a Bearer that is not ``pp_``/``ppat_``) it
decodes the token, rejects revoked sessions, loads the User, and sets:

  * ``workspace_id`` / ``user_id`` — the session's tenant, for OSS routers that
    must not trust an ``X-Workspace-Id`` header.
  * ``ee_user_authenticated`` — active users only (guests included). A
    limiter-only marker: on ``/api/v1/`` the OSS middleware keys its
    ``api_limiter`` on ``user:<user_id>`` instead of the client IP. It grants
    no access and is no exemption from the limiter.
  * ``full_access`` — platform admins (``is_superuser``) ONLY. It is the OSS
    superuser bypass that skips every ``require_scope`` check, so it is never
    derived from a workspace role (the W4b escalation fix).

Everything is best-effort: a bad, expired or revoked token just leaves state
untouched and the route's own auth decides.

Per-request scope (``_request_scope``, one ``_RequestScope`` per HTTP request,
closed and reset when the request finishes):

  * The user stash. After full verification of an ACTIVE user the bridge stores
    ``(token, user)``; ``RevocableJWTStrategy.read_token`` asks ``stashed_user``
    and gets it back only for the identical token string under the same key,
    audience and algorithm. That turns the route's fastapi-users dependencies
    (two when a route mixes ``current_optional_user`` and
    ``current_active_user``) from a full re-verify each into a lookup. Only the
    bridge writes it.
  * ``request_memo`` — a per-request cache for idempotent reads, used for the
    Workspace plan/overrides lookups. GET/HEAD requests only: a write request
    could read, change and re-read the same document. Successful results only.

The scope is a mutable object set before the inner app runs, so child tasks
(BaseHTTPMiddleware layers inside copy the context) see it; closing it at
request end means a task that outlives the request reads nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Hashable
from contextvars import ContextVar
from typing import Any

import jwt
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

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

_MEMO_METHODS = frozenset({"GET", "HEAD"})


class _RequestScope:
    """State for ONE HTTP request. Created and closed by the bridge only."""

    __slots__ = ("memo", "open", "token", "user", "verifier")

    def __init__(self, memo: dict[Hashable, Any] | None) -> None:
        self.memo = memo
        self.open = True
        self.token: str | None = None
        self.user: Any = None
        self.verifier: tuple[Any, ...] | None = None

    def close(self) -> None:
        self.open = False
        self.token = self.user = self.verifier = None
        self.memo = None


_request_scope: ContextVar[_RequestScope | None] = ContextVar("ee_request_scope", default=None)


def _verifier_of(strategy: Any) -> tuple[Any, ...]:
    """What a token was verified under: key, audience, algorithm."""
    key = strategy.decode_key
    if not isinstance(key, str):
        key = key.get_secret_value()
    return (key, tuple(strategy.token_audience), strategy.algorithm)


def stashed_user(token: str, strategy: Any) -> Any:
    """The user the bridge verified for ``token`` in THIS request, else None."""
    scope = _request_scope.get()
    if scope is None or not scope.open or scope.user is None or scope.token != token:
        return None
    if scope.verifier != _verifier_of(strategy):
        return None
    return scope.user


async def request_memo(key: Hashable, fetch: Callable[[], Awaitable[Any]]) -> Any:
    """``await fetch()``, cached for the rest of this GET/HEAD request.

    Outside a request, on a write method, or after the request ended it just
    calls ``fetch``. An exception is not cached; ``None`` is.
    """
    scope = _request_scope.get()
    memo = scope.memo if scope is not None and scope.open else None
    if memo is None:
        return await fetch()
    if key in memo:
        return memo[key]
    value = await fetch()
    memo[key] = value
    return value


class EEAuthBridgeMiddleware:
    """Stamp EE-authenticated requests for OSS and open the per-request scope."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_scope = _RequestScope({} if scope["method"] in _MEMO_METHODS else None)
        reset = _request_scope.set(request_scope)
        try:
            await self._stamp(Request(scope), request_scope)
            await self.app(scope, receive, send)
        finally:
            request_scope.close()
            _request_scope.reset(reset)

    async def _stamp(self, request: Request, request_scope: _RequestScope) -> None:
        path = request.url.path
        if path == "/" or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            return

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
            return

        resolved = await _resolve_user(token)
        if resolved is None:
            return
        user, verifier = resolved

        # Stamp the SESSION's tenant so OSS-package routers mounted under
        # /api/v1/ can scope themselves without importing pocketpaw_ee (the
        # open-core import boundary forbids it, and an import-linter contract
        # enforces that). src/pocketpaw/api/v1/cloud_projects.py once read its
        # tenant from an X-Workspace-Id header, which the caller chooses; these
        # two attributes are the supported way for an OSS router to get an
        # authenticated tenant. Set for EVERY resolved user, not only superusers.
        request.state.workspace_id = getattr(user, "active_workspace", None)
        request.state.user_id = str(getattr(user, "id", "") or "") or None

        # Limiter-only marker (see module docstring). Inactive users stay on the
        # per-IP bucket like anonymous callers, and are never stashed.
        if getattr(user, "is_active", False):
            request.state.ee_user_authenticated = True
            request_scope.token = token
            request_scope.user = user
            request_scope.verifier = verifier

        # full_access is the OSS superuser bypass — reserve it for genuine
        # platform administrators. A workspace owner/admin stays subject to OSS
        # require_scope like everyone else.
        if getattr(user, "is_superuser", False):
            request.state.full_access = True


async def _resolve_user(token: str) -> tuple[Any, tuple[Any, ...]] | None:
    """Verify the JWT and load the User. ``(user, verifier)`` or None on any failure."""
    try:
        # Lazy imports — keeps middleware module light and avoids triggering
        # the EE auth chain on processes that don't mount the cloud.
        from pocketpaw_ee.cloud.auth import sessions as sessions_service
        from pocketpaw_ee.cloud.auth.core import SECRET, RevocableJWTStrategy
        from pocketpaw_ee.cloud.models.user import User

        strategy = RevocableJWTStrategy(secret=SECRET, lifetime_seconds=1)
        verifier = _verifier_of(strategy)
        try:
            payload = jwt.decode(
                token,
                verifier[0],
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
        user = await User.get(user_id)
        if user is None:
            return None
        return user, verifier
    except Exception:
        # Swallow — bridge auth is best-effort. A failure here just means the
        # request is not stamped; the route's own auth still runs.
        logger.debug("EE auth bridge failed to resolve user", exc_info=True)
        return None
