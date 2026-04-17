# Chat router — send, stream (SSE), stop.
# Created: 2026-02-20
# Updated: 2026-03-09 — Reduce blocking chat timeout from 3600s to 300s
# Updated: 2026-02-25 — Tighten SSE session filter: block events without session_key
#   instead of silently passing them through to all clients.
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

# Active SSE sessions — maps safe_key → asyncio.Event for cancellation
_active_streams: dict[str, asyncio.Event] = {}

_WS_PREFIX = "websocket_"


def _extract_chat_id(session_id: str | None) -> str:
    """Convert a client-supplied session_id to a raw chat_id for the message bus.

    The client sends safe_key format (``websocket_<id>``).  We strip the prefix
    to obtain the raw id that becomes ``InboundMessage.chat_id``, so that
    ``session_key = "websocket:<id>"`` and the file on disk is
    ``sessions/websocket_<id>.json``.

    For new conversations (no session_id) we generate a short random hex id.
    """
    if not session_id:
        return uuid.uuid4().hex[:12]
    if session_id.startswith(_WS_PREFIX):
        return session_id[len(_WS_PREFIX) :]
    return session_id


def _to_safe_key(chat_id: str) -> str:
    """Build the safe_key that the client stores as its session identifier."""
    return f"{_WS_PREFIX}{chat_id}"


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
            logger.debug(
                "Bridge[%s] got outbound: chunk=%s end=%s content_len=%d",
                self.chat_id,
                msg.is_stream_chunk,
                msg.is_stream_end,
                len(msg.content),
            )
            if msg.is_stream_chunk:
                chunk = {"event": "chunk", "data": {"content": msg.content, "type": "text"}}
                await self.queue.put(chunk)
            elif msg.is_stream_end:
                await self.queue.put(
                    {
                        "event": "stream_end",
                        "data": {
                            "session_id": _to_safe_key(self.chat_id),
                            "usage": msg.metadata.get("usage", {}),
                        },
                    }
                )
            else:
                chunk = {"event": "chunk", "data": {"content": msg.content, "type": "text"}}
                await self.queue.put(chunk)

        async def _on_system(evt: SystemEvent) -> None:
            data = evt.data or {}
            # Filter out events belonging to other sessions.
            # session_key format is "channel:chat_id" (see InboundMessage.session_key).
            # Events without a session_key are dropped — they are global events
            # (health, daemon) that don't belong in a chat SSE stream.
            sk = data.get("session_key", "")
            if not sk or not sk.endswith(f":{self.chat_id}"):
                return
            if evt.event_type == "tool_start":
                await self.queue.put(
                    {
                        "event": "tool_start",
                        "data": {
                            "tool": data.get("name", ""),
                            "input": data.get("params", {}),
                        },
                    }
                )
            elif evt.event_type == "tool_result":
                await self.queue.put(
                    {
                        "event": "tool_result",
                        "data": {
                            "tool": data.get("name", ""),
                            "output": data.get("result", ""),
                        },
                    }
                )
            elif evt.event_type == "thinking":
                await self.queue.put(
                    {"event": "thinking", "data": {"content": data.get("content", "")}}
                )
            elif evt.event_type == "ask_user_question":
                await self.queue.put(
                    {
                        "event": "ask_user_question",
                        "data": {
                            "question": data.get("question", ""),
                            "options": data.get("options", []),
                        },
                    }
                )
            elif evt.event_type == "pocket_created":
                sk = data.get("session_key", "")
                safe_key = sk.replace(":", "_") if sk else ""
                await self.queue.put(
                    {
                        "event": "pocket_created",
                        "data": {
                            "spec": data.get("spec", {}),
                            "session_id": safe_key,
                            "pocket_cloud_id": data.get("pocket_cloud_id"),
                        },
                    }
                )
            elif evt.event_type == "pocket_mutation":
                await self.queue.put(
                    {
                        "event": "pocket_mutation",
                        "data": {"mutation": data.get("mutation", {})},
                    }
                )
            elif evt.event_type == "error":
                await self.queue.put(
                    {"event": "error", "data": {"detail": data.get("message", "")}}
                )

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

    chat_id = _extract_chat_id(chat_request.session_id)

    meta: dict = {"source": "rest_api"}
    if chat_request.file_context:
        meta["file_context"] = chat_request.file_context.model_dump(exclude_none=True)

    msg = InboundMessage(
        channel=Channel.WEBSOCKET,
        sender_id="api_client",
        chat_id=chat_id,
        content=chat_request.content,
        media=chat_request.media,
        metadata=meta,
    )
    bus = get_message_bus()
    await bus.publish_inbound(msg)
    return chat_id


@router.post("/chat", response_model=ChatResponse)
async def chat_send(body: ChatRequest):
    """Send a message and get the complete response (non-streaming)."""
    chat_id = _extract_chat_id(body.session_id)
    bridge = _APISessionBridge(chat_id)
    await bridge.start()

    await _send_message(
        ChatRequest(
            content=body.content,
            session_id=chat_id,
            media=body.media,
            file_context=body.file_context,
        )
    )

    # Collect all chunks until stream_end
    full_content = []
    usage = {}
    try:
        while True:
            try:
                # 5 min timeout per chunk — generous for tool use but won't
                # hang the client for an hour on failure.  Streaming endpoint
                # is preferred for long-running agent tasks.
                event = await asyncio.wait_for(bridge.queue.get(), timeout=300)
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
        session_id=_to_safe_key(chat_id),
        content="".join(full_content),
        usage=usage,
    )


@router.post("/chat/stream")
async def chat_stream(body: ChatRequest):
    """Send a message and receive SSE stream back."""
    chat_id = _extract_chat_id(body.session_id)
    safe_key = _to_safe_key(chat_id)

    # ── Cancel any in-flight stream for this session ──────────────
    old_cancel = _active_streams.pop(safe_key, None)
    if old_cancel:
        old_cancel.set()  # Signal old SSE generator to stop

        # Only cancel the agent task when there really IS a competing
        # stream — otherwise we'd kill a task that's just finishing up
        # memory storage for the previous (completed) message.
        try:
            from pocketpaw.dashboard_state import agent_loop

            session_key = f"websocket:{chat_id}"
            agent_loop.cancel_task(session_key)
        except Exception:
            logger.debug("Could not cancel stale agent task", exc_info=True)

        # Brief yield so the old generator's finally-block can unsubscribe
        # its bridge from the bus before the new bridge subscribes.
        await asyncio.sleep(0.05)

    cancel_event = asyncio.Event()
    _active_streams[safe_key] = cancel_event

    logger.info(
        "SSE stream: chat_id=%s safe_key=%s had_old_stream=%s",
        chat_id,
        safe_key,
        old_cancel is not None,
    )

    bridge = _APISessionBridge(chat_id)
    await bridge.start()

    # Send the inbound message
    await _send_message(
        ChatRequest(
            content=body.content,
            session_id=chat_id,
            media=body.media,
            file_context=body.file_context,
        )
    )

    async def _event_generator():
        try:
            # Initial event — use safe_key so client has a consistent session id
            yield f"event: stream_start\ndata: {json.dumps({'session_id': safe_key})}\n\n"

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
            _active_streams.pop(safe_key, None)

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

    # Accept both safe_key ("websocket_abc") and raw chat_id ("abc") formats
    cancel_event = _active_streams.get(session_id)
    if cancel_event is None and not session_id.startswith(_WS_PREFIX):
        cancel_event = _active_streams.get(_to_safe_key(session_id))
    if cancel_event is None:
        raise HTTPException(status_code=404, detail="No active stream for this session")

    cancel_event.set()

    # Also cancel the agent loop's processing task
    try:
        from pocketpaw.dashboard_state import agent_loop

        # Derive chat_id from whatever format was given
        raw = session_id
        if raw.startswith(_WS_PREFIX):
            raw = raw[len(_WS_PREFIX) :]
        agent_loop.cancel_task(f"websocket:{raw}")
    except Exception:
        pass

    return {"status": "ok", "session_id": session_id}
