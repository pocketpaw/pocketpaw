"""Run streaming + control endpoints."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.dto import StopRunResponse
from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cloud Agent Chat"], dependencies=[Depends(require_license)])


def _sse(entry_id: str, event: str, data: dict) -> bytes:
    return f"id: {entry_id}\nevent: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def _authorize(run_id: str, workspace_id: str, user_id: str):
    # Raises ``NotFound`` for missing, cross-tenant, AND cross-user runs.
    # Same response for all three so we don't leak run existence to a
    # workspace teammate who didn't own the run.
    doc = await run_service.get_run(run_id)
    if doc.workspace != workspace_id or doc.user_id != user_id:
        raise NotFound("chat_run", run_id)
    return doc


@router.get("/cloud/chat/runs/{run_id}/stream")
async def get_run_stream(
    run_id: str,
    after: str = Query("0"),
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> StreamingResponse:
    doc = await _authorize(run_id, workspace_id, user_id)
    transport = get_stream_transport()

    async def gen() -> AsyncIterator[bytes]:
        cursor = after
        # Only fall back to Mongo if the run is terminal AND the stream is
        # gone. For queued/running runs, XREAD BLOCK on a not-yet-created key
        # waits for the writer — avoids the POST→GET race where the executor
        # hasn't XADD'd its first event yet.
        is_terminal = doc.status not in ("queued", "running")
        if is_terminal and not await transport.stream_exists(run_id):
            yield _sse(
                "0-0",
                "stream_end",
                {
                    "assistant_message_id": doc.assistant_message_id,
                    "cancelled": doc.status in ("cancelled", "interrupted"),
                    "from_history": True,
                },
            )
            return
        while True:
            saw_terminal = False
            async for ev in transport.read_events(run_id, after=cursor, block_ms=15000):
                cursor = ev.entry_id
                yield _sse(ev.entry_id, ev.event, ev.data)
                if ev.is_terminal:
                    saw_terminal = True
            if saw_terminal:
                return
            # heartbeat so proxies keep the connection open
            yield b": ping\n\n"

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
