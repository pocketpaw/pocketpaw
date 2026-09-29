"""Pure-ASGI middleware that times every HTTP request and logs it to ``request_logs``.

Each request is timed to its response headers (so a streamed response counts
its time to first byte, not its stream length) and that sample goes to
``_core.timing`` for ``GET /api/v1/_admin/perf``. Every request not on the
skip list below is also recorded in the dedicated ``request_logs`` collection
(method + route template, status, duration, actor, workspace, ``is_error`` for
4xx/5xx), which powers the /audit page. Actor and workspace are read from the
shared ``request.state`` at response start, after the inner auth bridge ran. It
is NOT the workspace audit, so API traffic stays out of the Activity feed.
Websockets and lifespan pass through.

Skipped from the log (still timed): health probes, the CSRF token fetch and
static assets. They carry no audit value and were the bulk of the volume.

Writes are batched and bounded. Entries go onto a queue of ``_QUEUE_MAX``;
ONE consumer task drains it into ``insert_many`` (up to ``_BATCH_MAX``,
lingering ``_LINGER_SECONDS`` so a trickle still batches). Past the ceiling an
entry is dropped and counted: when Mongo is not keeping up, shedding telemetry
is the right thing to give up, and an unbounded backlog is how a Mongo stall
becomes an OOM. The consumer is strongly referenced (the loop keeps only a weak
one), bound to the loop that created it and rebuilt if the loop changes, and
``shutdown_request_log`` flushes the tail on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import time

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pocketpaw_ee.cloud._core import timing

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


class RequestLogMiddleware:
    """Times every HTTP request and logs the non-skipped ones to ``request_logs``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        # Checked against the raw path because the route template is only
        # known after the response, and this saves the actor resolution too.
        skipped = _is_skipped(request.url.path)
        start = time.perf_counter()

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                _on_response_start(request, skipped, start, message["status"])
            await send(message)

        await self.app(scope, receive, _send)


def _on_response_start(request: Request, skipped: bool, start: float, status_code: int) -> None:
    duration_ms = (time.perf_counter() - start) * 1000.0
    scope_route = request.scope.get("route")
    timing.record(request.method, scope_route, duration_ms)
    if skipped:
        return

    # Prefer the matched route template so we don't get one entry
    # per dynamic id (e.g. /workspaces/{id} vs /workspaces/abc).
    path = (
        scope_route.path
        if scope_route is not None and hasattr(scope_route, "path")
        else request.url.path
    )

    # Queued for the batching consumer; never blocks the response.
    _log_request(
        method=request.method,
        path=path,
        status_code=status_code,
        duration_ms=duration_ms,
        # Resolved here, not on the way in: the auth bridge that stamps the
        # user runs INSIDE this middleware, and request.state is shared.
        actor_id=_resolve_actor(request),
        workspace_id=_resolve_workspace(request),
        is_error=status_code >= 400,
        user_agent=request.headers.get("user-agent", ""),
        ip=request.client.host if request.client else None,
    )


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
