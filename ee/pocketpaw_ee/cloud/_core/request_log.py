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

Updated 2026-09-04 - the writes are batched.

Skipping the no-audit-value paths reduced the volume; it did not change the
shape. Every remaining request still opened its own insert, so at 100 req/s
this collection was still issuing 100 writes a second against the same
connection pool and the same WiredTiger cache as the traffic it was describing.

Entries now go onto a bounded queue that ONE consumer task drains into
``insert_many``. Three consequences worth knowing:

  * A burst becomes one round trip instead of hundreds. The consumer takes
    everything already queued, waits ``_LINGER_SECONDS`` for a trickle to
    become a batch too, and writes the lot.
  * Telemetry is visible on /audit up to that linger later than it was. It is
    telemetry about requests that have already been answered; the delay costs
    nothing and the batching is the point.
  * The queue is the backpressure. Past ``_QUEUE_MAX`` queued entries the
    entry is dropped and counted, exactly as the old in-flight ceiling did:
    when the database is not keeping up, shedding telemetry is the correct
    thing to give up, and queueing it without bound is how a Mongo stall
    became an OOM.

The queue and its consumer are bound to the loop that created them and rebuilt
if that loop changes, so a test that makes and tears down loops does not
inherit a consumer parked on a dead one.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

#: Ceiling on QUEUED telemetry entries. Past this, drop rather than queue: an
#: unbounded backlog of pending inserts is how a Mongo stall becomes an OOM.
#: Higher than the old in-flight ceiling because a queued dict is far cheaper
#: than a pending task, and the whole point is to absorb a burst.
_QUEUE_MAX = 4096

#: Most entries in one ``insert_many``. Bounds the BSON of a single write so a
#: backlog is drained in several round trips rather than one enormous one.
_BATCH_MAX = 200

#: How long the consumer waits for more entries once it has at least one.
#: Straight-line request rate is what makes batching worth anything, and at a
#: steady 100 req/s each insert would otherwise carry exactly one row again.
_LINGER_SECONDS = 0.25

#: Bound to the loop that created them; rebuilt if the running loop changes.
_queue: asyncio.Queue[dict] | None = None
_queue_loop: asyncio.AbstractEventLoop | None = None
_drain_task: asyncio.Task | None = None

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
    """Queue one ``request_logs`` entry for the batching consumer.

    Logs EVERY HTTP request that was not skipped above - including
    non-workspace endpoints like login - so they still appear in the global
    request-log view on the /audit page. Non-workspace requests are stored with
    an empty ``workspace`` field.

    Returns as soon as the entry is queued; the write itself happens on the
    consumer task, so nothing here is on the caller's critical path. Past the
    queue ceiling the entry is dropped and counted - see the module docstring.
    """
    global _dropped

    queue = _ensure_consumer()
    if queue is None:
        # No running loop (sync test harness, shutdown). Telemetry is not
        # worth raising over.
        return

    try:
        queue.put_nowait(
            {
                "workspace": workspace_id,
                "actor_id": actor_id,
                "method": method,
                "path": path,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 1),
                "is_error": is_error,
                "ip": ip,
                "user_agent": user_agent,
            }
        )
    except asyncio.QueueFull:
        # The database is not keeping up. Shedding telemetry is the right thing
        # to give up here; queueing it without bound is not.
        _dropped += 1
        if _dropped % 1000 == 1:
            logger.warning(
                "request-log telemetry dropped: %d entries shed with %d queued "
                "(ceiling %d). The request_logs write path is not keeping up.",
                _dropped,
                queue.qsize(),
                _QUEUE_MAX,
            )


def _ensure_consumer() -> asyncio.Queue[dict] | None:
    """Return the queue for the running loop, starting the consumer if needed.

    ``None`` when there is no running loop at all. The loop identity check is
    what stops a consumer parked on a torn-down test loop from swallowing the
    next one's entries: a queue belongs to the loop whose futures it holds, and
    handing it entries from another loop is not recoverable.
    """
    global _queue, _queue_loop, _drain_task

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    if _queue is None or _queue_loop is not loop or _drain_task is None or _drain_task.done():
        _retire(_drain_task)
        _queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        _queue_loop = loop
        _drain_task = loop.create_task(_drain(_queue))
    return _queue


def _retire(task: asyncio.Task | None) -> None:
    """Cancel a superseded consumer, but only if its own loop still runs.

    Rebuilding without this leaves the old consumer parked on a queue nobody
    feeds, and CPython raises out of its finalizer once that loop is gone.
    ``get_loop()`` rather than the running loop, because the case that gets
    here is precisely the one where those two differ.
    """
    if task is None or task.done():
        return
    try:
        if not task.get_loop().is_closed():
            task.cancel()
    except Exception:  # noqa: BLE001
        logger.debug("could not retire the previous request-log consumer", exc_info=True)


async def _collect_batch(queue: asyncio.Queue[dict]) -> list[dict]:
    """Block for one entry, then gather as many more as the ceiling allows.

    Two stages, and the first is what does the work under load. Everything
    ALREADY queued joins the batch immediately - which is exactly the backlog
    a slow write left behind. The linger that follows is for the opposite
    case: a steady trickle where nothing is waiting yet, and one insert per
    request is what we are trying to stop doing.
    """
    batch = [await queue.get()]
    while len(batch) < _BATCH_MAX:
        try:
            batch.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break

    if len(batch) >= _BATCH_MAX or _LINGER_SECONDS <= 0:
        return batch

    deadline = time.monotonic() + _LINGER_SECONDS
    while len(batch) < _BATCH_MAX:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            batch.append(await asyncio.wait_for(queue.get(), remaining))
        except TimeoutError:
            break
    return batch


async def _drain(queue: asyncio.Queue[dict]) -> None:
    """Consume the queue forever, writing each batch in one round trip.

    Never lets a write failure end the loop. A consumer that dies takes every
    subsequent request log with it and does so silently, which is a worse
    outcome than any single failed batch - and ``record_many`` already
    swallows its own errors, so reaching the ``except`` here means something
    unanticipated.
    """
    while True:
        batch = await _collect_batch(queue)
        try:
            # Imported per batch, not once at task start: an import that fails
            # at start would kill the consumer, and _ensure_consumer would then
            # respawn a dying task on every single request. Failing here logs
            # once per batch and keeps draining.
            from pocketpaw_ee.cloud.request_log import service as _request_log_service

            await _request_log_service.record_many(batch)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("request-log batch of %d failed to write", len(batch), exc_info=True)


async def shutdown_request_log(timeout: float = 5.0) -> int:
    """Flush what is queued and stop the consumer. Returns entries written.

    Called from the cloud app's shutdown hook. Without it the queued tail is
    simply lost on every deploy, which is a visible gap in /audit right at the
    moment - a restart - when someone is most likely to be reading it.
    """
    global _queue, _queue_loop, _drain_task

    queue, task = _queue, _drain_task
    _queue, _queue_loop, _drain_task = None, None, None

    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("request-log consumer raised on shutdown", exc_info=True)

    if queue is None or queue.empty():
        return 0

    from pocketpaw_ee.cloud.request_log import service as _request_log_service

    written = 0
    try:
        async with asyncio.timeout(timeout):
            while not queue.empty():
                batch: list[dict] = []
                while len(batch) < _BATCH_MAX and not queue.empty():
                    batch.append(queue.get_nowait())
                written += await _request_log_service.record_many(batch)
    except TimeoutError:
        logger.warning(
            "request-log shutdown flush timed out with %d entries unwritten", queue.qsize()
        )
    return written


def _reset_for_tests() -> None:
    """Drop the consumer and the queue without flushing."""
    global _queue, _queue_loop, _drain_task, _dropped

    _retire(_drain_task)
    _queue, _queue_loop, _drain_task = None, None, None
    _dropped = 0


__all__ = ["RequestLogMiddleware", "shutdown_request_log"]
