"""Run streaming + control endpoints.

Changes:
- 2026-09-27 (fix/run-stream-session-readers) — the stream admits a reader of
  the run's thread, not only the run's author. Pocket and Paw Site threads are
  readable by everyone who may read the pocket (#2244), so a teammate opening a
  thread mid-turn saw the history but 404'd on the live reply. ``_authorize``
  keeps its author-only rule for stop; ``_authorize_read`` adds the thread
  readers from ``sessions_service.can_read_session`` for session-scoped runs.
- 2026-09-04 (fix/unblock-event-loop, backend-perf M7) — the stream loop has a
  maximum lifetime. It previously had none: the only exits were a terminal
  event or a failed ``yield`` (which is how a client disconnect is noticed).
  A run that never writes its terminal frame therefore heartbeated forever,
  and the ``stream_exists`` fallback below does not catch that case — it only
  fires when the events key is GONE, whereas a worker OOM-killed mid-run
  leaves the key present and unfinished. Each abandoned subscription costs a
  live asyncio task plus a Redis connection blocked in 15s slices, on a client
  pool with no configured cap. The ceiling is derived from the run's own
  ``POCKETPAW_CLOUD_RUN_JOB_TIMEOUT`` so the two cannot drift apart.
- 2026-06-10 (sov/w3a-igw — per-run token metering) — the history-replay
  ``stream_end`` frame (served when a terminal run's live stream has expired) now
  carries the persisted ``doc.usage`` so a reconnecting client still gets the
  run's real token counts. Live frames already carry usage from ``run_core``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import stream_max_lifetime_seconds
from pocketpaw_ee.cloud.chat.runs.dto import StopRunResponse
from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport, sse_frame, sse_tail
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cloud Agent Chat"], dependencies=[Depends(require_license)])


async def _authorize(run_id: str, workspace_id: str, user_id: str):
    # Raises ``NotFound`` for missing, cross-tenant, AND cross-user runs.
    # Same response for all three so we don't leak run existence to a
    # workspace teammate who didn't own the run.
    doc = await run_service.get_run(run_id)
    if doc.workspace != workspace_id or doc.user_id != user_id:
        raise NotFound("chat_run", run_id)
    return doc


async def _authorize_read(run_id: str, workspace_id: str, user_id: str):
    # The author, or anyone who may read the thread the run belongs to. Only
    # session-scoped runs have a thread to share; the rest stay author-only.
    # Every refusal is the same NotFound as ``_authorize``.
    from pocketpaw_ee.cloud.sessions import service as sessions_service

    doc = await run_service.get_run(run_id)
    if doc.workspace != workspace_id:
        raise NotFound("chat_run", run_id)
    if doc.user_id == user_id:
        return doc
    if doc.context_type == "session" and await sessions_service.can_read_session(
        doc.scope_id, user_id
    ):
        return doc
    raise NotFound("chat_run", run_id)


@router.get("/cloud/chat/runs/{run_id}/stream")
async def get_run_stream(
    run_id: str,
    after: str = Query("0"),
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> StreamingResponse:
    doc = await _authorize_read(run_id, workspace_id, user_id)
    transport = get_stream_transport()

    async def gen() -> AsyncIterator[bytes]:
        cursor = after
        # Only fall back to Mongo if the run is terminal AND the stream is
        # gone. For queued/running runs, XREAD BLOCK on a not-yet-created key
        # waits for the writer — avoids the POST→GET race where the executor
        # hasn't XADD'd its first event yet.
        is_terminal = doc.status not in ("queued", "running")
        if is_terminal and not await transport.stream_exists(run_id):
            yield sse_frame(
                "0-0",
                "stream_end",
                {
                    "assistant_message_id": doc.assistant_message_id,
                    "cancelled": doc.status in ("cancelled", "interrupted"),
                    # Per-run token metering (W3a): replay the persisted usage so a
                    # client reconnecting after the live stream expired still gets
                    # the run's real token counts, not an empty dict.
                    "usage": getattr(doc, "usage", {}) or {},
                    "from_history": True,
                },
            )
            return
        # Hard ceiling on this subscription (see ``sse_tail``): a run whose
        # terminal event never arrives (the worker was OOM-killed AFTER the
        # events key existed, so the ``stream_exists`` fallback above does not
        # trigger) would otherwise heartbeat forever.
        deadline = time.monotonic() + stream_max_lifetime_seconds()
        async for chunk in sse_tail(transport, run_id, cursor, deadline):
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cloud/chat/runs/{run_id}/stop")
async def post_run_stop(
    run_id: str,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> StopRunResponse:
    await _authorize(run_id, workspace_id, user_id)
    await get_stream_transport().request_cancel(run_id)
    return StopRunResponse()
