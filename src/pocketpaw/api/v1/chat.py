# Chat router — send, stream (SSE), stop.
# Created: 2026-02-20
#
# Enables external clients to send messages and receive responses via HTTP.
# SSE streaming reuses the entire AgentLoop pipeline via _APISessionBridge.

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from pocketpaw.api.deps import require_scope
from pocketpaw.api.v1.schemas.chat import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"], dependencies=[Depends(require_scope("chat"))])

# Active SSE sessions — maps session_id → asyncio.Event for cancellation
_active_streams: dict[str, asyncio.Event] = {}


class _APISessionBridge:
    """Bridges the message bus to an asyncio.Queue for SSE streaming.

    Subscribes to OutboundMessage and SystemEvent for a specific chat_id,
    converts them to SSE event dicts, and yields them to the client.
    """

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self._outbound_cb = None
        self._system_cb = None

    async def start(self) -> None:
        """Subscribe to the message bus for this session."""
        from pocketpaw.bus import get_message_bus
        from pocketpaw.bus.events import Channel, OutboundMessage, SystemEvent

        bus = get_message_bus()

        async def _on_outbound(msg: OutboundMessage) -> None:
            if msg.chat_id != self.chat_id:
                return
            if msg.is_stream_chunk:
                chunk = {"event": "chunk", "data": {"content": msg.content, "type": "text"}}
                await self.queue.put(chunk)
            elif msg.is_stream_end:
                await self.queue.put(
                    {
                        "event": "stream_end",
                        "data": {
                            "session_id": self.chat_id,
                            "usage": msg.metadata.get("usage", {}),
                        },
                    }
                )
            else:
                chunk = {"event": "chunk", "data": {"content": msg.content, "type": "text"}}
                await self.queue.put(chunk)

        async def _on_system(evt: SystemEvent) -> None:
            meta = evt.metadata or {}
            if meta.get("chat_id") and meta["chat_id"] != self.chat_id:
                return
            if evt.event_type == "tool_start":
                await self.queue.put(
                    {
                        "event": "tool_start",
                        "data": {"tool": meta.get("tool", ""), "input": meta.get("input", {})},
                    }
                )
            elif evt.event_type == "tool_result":
                await self.queue.put(
                    {
                        "event": "tool_result",
                        "data": {"tool": meta.get("tool", ""), "output": evt.content},
                    }
                )
            elif evt.event_type == "thinking":
                await self.queue.put({"event": "thinking", "data": {"content": evt.content}})
            elif evt.event_type == "error":
                await self.queue.put({"event": "error", "data": {"detail": evt.content}})

        self._outbound_cb = _on_outbound
        self._system_cb = _on_system
        bus.subscribe_outbound(Channel.WEBSOCKET, _on_outbound)
        bus.subscribe_system(_on_system)

    async def stop(self) -> None:
        """Unsubscribe from the message bus."""
        from pocketpaw.bus import get_message_bus
        from pocketpaw.bus.events import Channel

        bus = get_message_bus()
        if self._outbound_cb:
            bus.unsubscribe_outbound(Channel.WEBSOCKET, self._outbound_cb)
        if self._system_cb:
            bus.unsubscribe_system(self._system_cb)


async def _send_message(chat_request: ChatRequest) -> str:
    """Publish an inbound message to the bus and return the chat_id."""
    from pocketpaw.bus import get_message_bus
    from pocketpaw.bus.events import Channel, InboundMessage

    chat_id = chat_request.session_id or f"api:{uuid.uuid4().hex[:12]}"

    msg = InboundMessage(
        channel=Channel.WEBSOCKET,
        sender_id="api_client",
        chat_id=chat_id,
        content=chat_request.content,
        media=chat_request.media,
        metadata={"source": "rest_api"},
    )
    bus = get_message_bus()
    await bus.publish_inbound(msg)
    return chat_id


@router.post("/chat", response_model=ChatResponse)
async def chat_send(body: ChatRequest):
    """Send a message and get the complete response (non-streaming)."""
    bridge = _APISessionBridge(body.session_id or f"api:{uuid.uuid4().hex[:12]}")
    await bridge.start()

    chat_id = await _send_message(
        ChatRequest(content=body.content, session_id=bridge.chat_id, media=body.media)
    )

    # Collect all chunks until stream_end
    full_content = []
    usage = {}
    try:
        while True:
            try:
                event = await asyncio.wait_for(bridge.queue.get(), timeout=120)
            except TimeoutError:
                break

            if event["event"] == "chunk":
                full_content.append(event["data"].get("content", ""))
            elif event["event"] == "stream_end":
                usage = event["data"].get("usage", {})
                break
            elif event["event"] == "error":
                detail = event["data"].get("detail", "Agent error")
                raise HTTPException(status_code=500, detail=detail)
    finally:
        await bridge.stop()

    return ChatResponse(
        session_id=chat_id,
        content="".join(full_content),
        usage=usage,
    )


@router.post("/chat/stream")
async def chat_stream(body: ChatRequest):
    """Send a message and receive SSE stream back."""
    chat_id = body.session_id or f"api:{uuid.uuid4().hex[:12]}"
    cancel_event = asyncio.Event()
    _active_streams[chat_id] = cancel_event

    bridge = _APISessionBridge(chat_id)
    await bridge.start()

    # Send the inbound message
    await _send_message(ChatRequest(content=body.content, session_id=chat_id, media=body.media))

    async def _event_generator():
        try:
            # Initial event
            yield f"event: stream_start\ndata: {json.dumps({'session_id': chat_id})}\n\n"

            while not cancel_event.is_set():
                try:
                    event = await asyncio.wait_for(bridge.queue.get(), timeout=1.0)
                except TimeoutError:
                    continue

                yield f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"

                if event["event"] in ("stream_end", "error"):
                    break
        finally:
            await bridge.stop()
            _active_streams.pop(chat_id, None)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/stop")
async def chat_stop(session_id: str = ""):
    """Cancel an in-flight chat response."""
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    cancel_event = _active_streams.get(session_id)
    if cancel_event is None:
        raise HTTPException(status_code=404, detail="No active stream for this session")

    cancel_event.set()
    return {"status": "ok", "session_id": session_id}
