"""Middleware that logs every API request/response to the dedicated request_logs collection.

Every HTTP request that reaches a route handler is recorded as a
document in the ``request_logs`` MongoDB collection with:
  - HTTP method + path template (e.g. ``GET /workspaces/{id}/audit``)
  - Response status code
  - Duration in milliseconds
  - Authenticated actor (if any)

This powers the /audit page so workspace admins can see which endpoints
are being called, which are failing, and how long they take — without
needing a separate observability stack.

Uses a dedicated collection (NOT the workspace audit) so API traffic
doesn't pollute the Activity feed.

Failures (4xx/5xx) are flagged as ``is_error=True`` so they can be
filtered separately.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Logs every API request/response to the workspace audit store."""

    def __init__(self, app: FastAPI) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        start = time.perf_counter()

        # Read auth info before the response (the user may be resolved
        # by downstream middleware during request processing).
        actor_id = _resolve_actor(request)

        response: Response = await call_next(request)

        duration_ms = (time.perf_counter() - start) * 1000.0

        # Prefer the matched route template so we don't get one entry
        # per dynamic id (e.g. /workspaces/{id} vs /workspaces/abc).
        scope_route = request.scope.get("route")
        path = (
            scope_route.path
            if scope_route is not None and hasattr(scope_route, "path")
            else request.url.path
        )

        method = request.method
        status_code = response.status_code
        is_error = status_code >= 400

        # Fire-and-forget write to the workspace audit.  This runs after
        # the response has been sent so it never blocks the caller.
        _log_request(
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
            actor_id=actor_id,
            workspace_id=_resolve_workspace(request),
            is_error=is_error,
            user_agent=request.headers.get("user-agent", ""),
            ip=request.client.host if request.client else None,
        )

        return response


def _resolve_actor(request: Request) -> str:
    """Extract the authenticated actor from the request state.

    FastAPI / Starlette middlewares store the resolved user on
    ``request.state.user`` (set by AuthMiddleware / EEAuthBridge).
    Falls back to ``"anonymous"`` when no auth is present.
    """
    user = getattr(request.state, "user", None)
    if user is not None:
        uid = getattr(user, "id", None) or getattr(user, "sub", None)
        if uid:
            return str(uid)
    # Fallback: check for the user_id set by EEAuthBridgeMiddleware.
    uid = getattr(request.state, "user_id", None)
    if uid:
        return str(uid)
    return "anonymous"


def _resolve_workspace(request: Request) -> str:
    """Extract the workspace id from the request path or auth state.

    Workspace-scoped endpoints carry the workspace as a path parameter
    (``/{workspace_id}/...``). Falls back to workspace on request state.
    Returns ``""`` for endpoints that aren't workspace-scoped.
    """
    path_params = request.path_params
    ws = path_params.get("workspace_id")
    if ws:
        return str(ws)
    ws = getattr(request.state, "workspace_id", None)
    if ws:
        return str(ws)
    return ""


def _log_request(
    *,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    actor_id: str,
    workspace_id: str,
    is_error: bool,
    user_agent: str,
    ip: str | None,
) -> None:
    """Fire-and-forget write to the dedicated ``request_logs`` collection.

    Logs EVERY HTTP request — including non-workspace endpoints like
    health, login, CSRF, etc. Non-workspace requests are stored with
    an empty ``workspace`` field so they still appear in the global
    request-log view on the /audit page.

    Runs synchronously inside the async middleware but only does
    a quick ``ensure_future`` — the actual MongoDB write is deferred.
    If the write fails it's logged and swallowed.
    """
    import asyncio

    from pocketpaw_ee.cloud.request_log import service as _request_log_service

    asyncio.ensure_future(
        _request_log_service.record(
            workspace_id=workspace_id,
            actor_id=actor_id,
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=round(duration_ms, 1),
            is_error=is_error,
            ip=ip,
            user_agent=user_agent,
        )
    )


__all__ = ["RequestLogMiddleware"]
