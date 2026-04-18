"""Chat persistence bridge — saves runtime WebSocket messages to MongoDB.

Subscribes to the message bus outbound channel to persist agent responses.
User messages are persisted via save_user_message() called from the WS adapter.
This ensures all chat history is in MongoDB regardless of chat system.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Track active sessions: websocket chat_id → cloud session info
_active_sessions: dict[str, dict] = {}
# Accumulate streaming chunks per session
_stream_buffers: dict[str, str] = {}


def register_chat_persistence() -> None:
    """Subscribe to the message bus to persist outbound messages to MongoDB."""
    try:
        from pocketpaw.bus.queue import get_bus

        bus = get_bus()
        if bus is None:
            logger.debug("Message bus not available, chat persistence not registered")
            return

        from pocketpaw.bus.events import Channel

        bus.subscribe_outbound(Channel.WEBSOCKET, _on_outbound_message)
        logger.info("Chat persistence bridge registered")
    except Exception:
        logger.debug("Failed to register chat persistence", exc_info=True)


async def save_user_message(chat_id: str, content: str) -> None:
    """Called by the WebSocket adapter to persist a user message."""
    try:
        session_info = await _ensure_cloud_session(chat_id)
        if not session_info:
            return

        from ee.cloud.models.message import Message

        msg = Message(
            group=session_info["group_id"],
            sender=session_info["user_id"],
            sender_type="user",
            content=content,
        )
        await msg.insert()
    except Exception:
        logger.debug("Failed to persist user message", exc_info=True)


async def _on_outbound_message(message) -> None:
    """Accumulate agent stream chunks and save final message to MongoDB."""
    try:
        chat_id = message.chat_id

        if message.is_stream_chunk:
            _stream_buffers[chat_id] = _stream_buffers.get(chat_id, "") + (message.content or "")
            return

        if message.is_stream_end:
            full_text = _stream_buffers.pop(chat_id, "")
            if not full_text.strip():
                return

            session_info = await _ensure_cloud_session(chat_id)
            if not session_info:
                return

            from ee.cloud.models.message import Message

            msg = Message(
                group=session_info["group_id"],
                sender=None,
                sender_type="agent",
                content=full_text,
            )
            await msg.insert()

            # Touch session activity
            from ee.cloud.models.session import Session

            session_doc = await Session.find_one(Session.sessionId == f"websocket_{chat_id}")
            if session_doc:
                session_doc.lastActivity = datetime.now(UTC)
                session_doc.messageCount += 1
                await session_doc.save()
            return

        # Non-streaming content accumulation
        if message.content and not message.is_stream_chunk:
            _stream_buffers[chat_id] = _stream_buffers.get(chat_id, "") + (message.content or "")
    except Exception:
        logger.debug("Failed to persist outbound message", exc_info=True)


async def _ensure_cloud_session(chat_id: str) -> dict | None:
    """Find or create a cloud session + group for a runtime WebSocket chat."""
    if chat_id in _active_sessions:
        return _active_sessions[chat_id]

    try:
        from ee.cloud.models.group import Group
        from ee.cloud.models.session import Session

        session_id = f"websocket_{chat_id}"

        # Check if session already exists
        session = await Session.find_one(Session.sessionId == session_id)
        if session and session.group:
            info = {
                "session_id": str(session.id),
                "group_id": session.group,
                "user_id": session.owner,
            }
            _active_sessions[chat_id] = info
            return info

        # No session yet — we need a workspace context
        # Try to get the first available workspace
        from ee.cloud.models.user import User

        users = await User.find({"workspaces": {"$ne": []}}).limit(1).to_list()
        if not users:
            return None

        user = users[0]
        workspace_id = user.workspaces[0].workspace if user.workspaces else None
        if not workspace_id:
            return None

        user_id = str(user.id)

        # Create a runtime chat group if needed
        if not session:
            # Create group for this runtime session
            group = Group(
                workspace=workspace_id,
                name="PocketPaw Chat",
                type="dm",
                members=[user_id],
                owner=user_id,
            )
            await group.insert()

            session = Session(
                sessionId=session_id,
                workspace=workspace_id,
                owner=user_id,
                title="PocketPaw Chat",
                group=str(group.id),
            )
            await session.insert()

            info = {"session_id": str(session.id), "group_id": str(group.id), "user_id": user_id}
        else:
            # Session exists but no group — create one
            group = Group(
                workspace=workspace_id,
                name="PocketPaw Chat",
                type="dm",
                members=[user_id],
                owner=user_id,
            )
            await group.insert()
            session.group = str(group.id)
            await session.save()

            info = {"session_id": str(session.id), "group_id": str(group.id), "user_id": user_id}

        _active_sessions[chat_id] = info
        logger.info(
            "Created cloud session for runtime chat: %s → group %s", session_id, info["group_id"]
        )
        return info
    except Exception:
        logger.debug("Failed to ensure cloud session for %s", chat_id, exc_info=True)
        return None
