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

Updated 2026-09-04 — two production-shaped problems, both in the write path:

  1. ``asyncio.ensure_future`` returned a task nobody held a reference to.
     CPython's loop keeps only a WEAK reference, so a pending log write can be
     garbage-collected mid-await; the write simply vanishes, and non-
     deterministically. This module now holds strong references until each
     task completes — the same guard the codebase already applies in
     chat/ws.py and shared/agent_bridge.py.
  2. Nothing bounded how many of those could accumulate. If Mongo slows down,
     pending inserts pile up in the web process without limit, converting a
     database stall into unbounded memory growth instead of backpressure.
     There is now a ceiling; past it, telemetry is dropped and the drop is
     counted. Losing request telemetry is the correct thing to sacrifice when
     the database is already struggling.

Also: the highest-volume paths are now skipped rather than recorded. Request
volume and telemetry write volume were 1:1, which made ``request_logs`` a
strong candidate for the busiest write target in the database. Health probes,
the CSRF token fetch and static assets carry no audit value and are the bulk
of that traffic.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

#: Strong references to in-flight telemetry writes. Without this the loop's
#: weak reference is the only one and the task may be collected mid-await.
_pending: set[asyncio.Task] = set()

#: Ceiling on concurrent telemetry writes. Past this, drop rather than queue:
#: an unbounded pile of pending inserts is how a Mongo stall becomes an OOM.
_MAX_PENDING = 256

#: Suppressed so a dropped-telemetry incident is reported once, not per request.
_dropped = 0

#: Paths with no audit value that dominate request volume. Matched against the
#: raw URL path, so they are skipped before any actor/workspace resolution.
_SKIP_EXACT = frozenset(
    {
        "/health",
        "/version",
        "/api/v1/health",
        "/api/v1/version",
        "/api/v1/auth/csrf",
    }
)
_SKIP_PREFIXES = ("/static/", "/uploads/", "/assets/")


def _is_skipped(path: str) -> bool:
    return path in _SKIP_EXACT or path.startswith(_SKIP_PREFIXES)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Logs every API request/response to the workspace audit store."""

    def __init__(self, app: FastAPI) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        # Skip the high-volume, no-audit-value paths before doing any work.
        # Checked against the raw path because the route template is only
        # known after the response, and this saves the actor resolution too.
        if _is_skipped(request.url.path):
            return await call_next(request)

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

    Schedules the MongoDB write and returns; the write itself is deferred so
    it never blocks the caller. The task is held in ``_pending`` until it
    completes — see the module docstring for why a bare ``ensure_future`` was
    not safe — and dropped once the in-flight ceiling is reached.
    """
    global _dropped

    from pocketpaw_ee.cloud.request_log import service as _request_log_service

    if len(_pending) >= _MAX_PENDING:
        # The database is not keeping up. Shedding telemetry is the right
        # thing to give up here; queueing it without bound is not.
        _dropped += 1
        if _dropped % 1000 == 1:
            logger.warning(
                "request-log telemetry dropped: %d writes shed with %d in flight "
                "(ceiling %d). The request_logs write path is not keeping up.",
                _dropped,
                len(_pending),
                _MAX_PENDING,
            )
        return

    try:
        task = asyncio.create_task(
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
    except RuntimeError:
        # No running loop (sync test harness, shutdown). Telemetry is not
        # worth raising over.
        return

    _pending.add(task)
    task.add_done_callback(_pending.discard)


__all__ = ["RequestLogMiddleware"]
