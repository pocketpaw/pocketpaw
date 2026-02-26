# Events SSE router — real-time system events via Server-Sent Events.
# Created: 2026-02-20
# Updated: 2026-02-25 — Align SSE envelope shape with chat.py (use "event" key, not "event_type").
#
# External clients (e.g. Tauri desktop app) can subscribe to this endpoint
# instead of connecting to the WebSocket for real-time system events.

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Events"])


@router.get("/events/stream")
async def events_stream():
    """Subscribe to real-time system events via SSE.

    Delivers the same event types as the WebSocket connection:
    tool_start, tool_result, thinking, error, health_update, inbox_update, etc.
    """
    queue: asyncio.Queue = asyncio.Queue()
    cancel_event = asyncio.Event()

    async def _subscribe():
        from pocketpaw.bus import get_message_bus
        from pocketpaw.bus.events import SystemEvent

        bus = get_message_bus()

        async def _on_event(evt: SystemEvent) -> None:
            await queue.put({
                "event": evt.event_type,
                "data": evt.data or {},
            })

        bus.subscribe_system(_on_event)
        return _on_event

    sub = await _subscribe()

    async def _event_generator():
        try:
            # Initial heartbeat
            yield f"event: connected\ndata: {json.dumps({'status': 'ok'})}\n\n"

            while not cancel_event.is_set():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield (
                        f"event: {event['event']}\n"
                        f"data: {json.dumps(event)}\n\n"
                    )
                except TimeoutError:
                    # Send keepalive comment every 30s
                    yield ": keepalive\n\n"
        finally:
            from pocketpaw.bus import get_message_bus

            bus = get_message_bus()
            bus.unsubscribe_system(sub)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
