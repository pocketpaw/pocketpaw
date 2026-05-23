"""Run streaming + control endpoints.

GET  /cloud/chat/runs/{run_id}/stream?after=<entry_id>   resumable SSE
POST /cloud/chat/runs/{run_id}/stop                       request cancellation
"""

from __future__ import annotations

import asyncio
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


async def _authorize(run_id: str, workspace_id: str):
    """Load the run and confirm it belongs to the caller's workspace.

    Raises ``NotFound`` either when the run doesn't exist or when it
    belongs to a different workspace — we never leak the existence of
    cross-tenant runs.
    """
    doc = await run_service.get_run(run_id)  # raises NotFound
    if doc.workspace != workspace_id:
        raise NotFound("chat_run", run_id)
    return doc


@router.get("/cloud/chat/runs/{run_id}/stream")
async def get_run_stream(
    run_id: str,
    after: str = Query("0"),
    user_id: str = Depends(current_user_id),  # noqa: ARG001 — tenancy comes via workspace
    workspace_id: str = Depends(current_workspace_id),
) -> StreamingResponse:
    doc = await _authorize(run_id, workspace_id)
    transport = get_stream_transport()

    async def gen() -> AsyncIterator[bytes]:
        cursor = after
        # If the stream has already expired (or was never created), fall
        # back to the Mongo run doc and emit a single synthetic stream_end.
        if not await transport.stream_exists(run_id):
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
            # block timed out — heartbeat so proxies keep the connection open
            yield b": ping\n\n"
            await asyncio.sleep(0)

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
    user_id: str = Depends(current_user_id),  # noqa: ARG001 — tenancy comes via workspace
    workspace_id: str = Depends(current_workspace_id),
) -> StopRunResponse:
    await _authorize(run_id, workspace_id)
    await get_stream_transport().request_cancel(run_id)
    return StopRunResponse()
