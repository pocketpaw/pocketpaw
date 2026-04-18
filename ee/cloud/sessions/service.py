"""Sessions domain — business logic service."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from beanie import PydanticObjectId

from ee.cloud.models.session import Session
from ee.cloud.sessions.schemas import (
    CreateSessionRequest,
    UpdateSessionRequest,
)
from ee.cloud.shared.errors import Forbidden, NotFound
from ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


def _session_response(session: Session) -> dict:
    """Build a frontend-compatible dict from a Session document."""
    return {
        "_id": str(session.id),
        "sessionId": session.sessionId,
        "workspace": session.workspace,
        "owner": session.owner,
        "title": session.title,
        "pocket": session.pocket,
        "group": session.group,
        "agent": session.agent,
        "messageCount": session.messageCount,
        "lastActivity": session.lastActivity.isoformat() if session.lastActivity else None,
        "createdAt": session.createdAt.isoformat() if session.createdAt else None,
        "deletedAt": session.deleted_at.isoformat() if session.deleted_at else None,
    }


class SessionService:
    """Stateless service encapsulating session business logic."""

    @staticmethod
    async def create(workspace_id: str, user_id: str, body: CreateSessionRequest) -> dict:
        """Create a session, or update if sessionId already exists."""
        sid = body.session_id or f"websocket_{uuid.uuid4().hex[:12]}"

        # If linking to an existing runtime session, check if MongoDB record exists
        if body.session_id:
            existing = await Session.find_one(Session.sessionId == body.session_id)
            if existing:
                # Update the existing record (e.g. add pocket link)
                if body.pocket_id:
                    existing.pocket = body.pocket_id
                if body.title and body.title != "New Chat":
                    existing.title = body.title
                await existing.save()
                return _session_response(existing)

        session = Session(
            sessionId=sid,
            workspace=workspace_id,
            owner=user_id,
            title=body.title,
            pocket=body.pocket_id,
            group=body.group_id,
            agent=body.agent_id,
        )
        await session.insert()

        await event_bus.emit(
            "session.created",
            {
                "session_id": str(session.id),
                "session_uuid": session.sessionId,
                "workspace_id": workspace_id,
                "owner_id": user_id,
                "pocket_id": body.pocket_id,
            },
        )

        return _session_response(session)

    @staticmethod
    async def list_sessions(workspace_id: str, user_id: str) -> list[dict]:
        """List all sessions for user, sorted by lastActivity desc."""
        sessions = (
            await Session.find(
                Session.workspace == workspace_id,
                Session.owner == user_id,
                Session.deleted_at == None,  # noqa: E711
            )
            .sort(-Session.lastActivity)
            .to_list()
        )
        return [_session_response(s) for s in sessions]

    @staticmethod
    async def get(session_id: str, user_id: str) -> dict:
        session = await SessionService._get_session(session_id, user_id)
        return _session_response(session)

    @staticmethod
    async def update(session_id: str, user_id: str, body: UpdateSessionRequest) -> dict:
        session = await SessionService._get_session(session_id, user_id)
        if body.title is not None:
            session.title = body.title
        if body.pocket_id is not None:
            session.pocket = body.pocket_id
        await session.save()
        return _session_response(session)

    @staticmethod
    async def delete(session_id: str, user_id: str) -> None:
        session = await SessionService._get_session(session_id, user_id)
        session.deleted_at = datetime.now(UTC)
        await session.save()

    # -----------------------------------------------------------------
    # Pocket-scoped
    # -----------------------------------------------------------------

    @staticmethod
    async def list_for_pocket(pocket_id: str, user_id: str) -> list[dict]:
        logger.info(f"Listing sessions for pocket {pocket_id} and user {user_id}")
        sessions = (
            await Session.find(
                Session.pocket == pocket_id,
                Session.owner == user_id,
                Session.deleted_at == None,  # noqa: E711
            )
            .sort(-Session.lastActivity)
            .to_list()
        )
        return [_session_response(s) for s in sessions]

    @staticmethod
    async def create_for_pocket(
        workspace_id: str,
        user_id: str,
        pocket_id: str,
        body: CreateSessionRequest,
    ) -> dict:
        body_with_pocket = CreateSessionRequest(
            title=body.title,
            pocket_id=pocket_id,
            group_id=body.group_id,
            agent_id=body.agent_id,
            session_id=body.session_id,
        )
        return await SessionService.create(workspace_id, user_id, body_with_pocket)

    # -----------------------------------------------------------------
    # History
    # -----------------------------------------------------------------

    @staticmethod
    async def get_history(session_id: str, user_id: str) -> dict:
        """Get session chat history from runtime file memory."""
        session = await SessionService._get_session(session_id, user_id)

        # Try cloud Messages first (group chat)
        if session.group:
            try:
                from ee.cloud.models.message import Message

                messages = (
                    await Message.find(
                        Message.group == session.group,
                        Message.deleted == False,  # noqa: E711, E712
                    )
                    .sort("createdAt")
                    .limit(100)
                    .to_list()
                )
                if messages:
                    return {
                        "messages": [
                            {
                                "_id": str(m.id),
                                "role": "assistant" if m.sender_type == "agent" else "user",
                                "content": m.content,
                                "sender": m.sender,
                                "senderType": m.sender_type,
                                "createdAt": m.createdAt.isoformat() if m.createdAt else None,
                            }
                            for m in messages
                        ]
                    }
            except Exception:
                pass

        # Try runtime file-based memory
        try:
            from pocketpaw.memory.manager import MemoryManager

            manager = MemoryManager()
            sid = session.sessionId
            # Try all possible key formats
            for key in [sid, sid.replace("_", ":", 1), f"websocket:{sid}"]:
                try:
                    entries = await manager.get_session_history(key)
                    if entries:
                        return {"messages": entries}
                except Exception:
                    continue
        except Exception:
            pass

        return {"messages": []}

    # -----------------------------------------------------------------
    # Touch
    # -----------------------------------------------------------------

    @staticmethod
    async def touch(session_id: str) -> None:
        """Update lastActivity and increment messageCount."""
        session = await Session.find_one(Session.sessionId == session_id)
        # Fallback: strip websocket_ prefix
        if not session and session_id.startswith("websocket_"):
            session = await Session.find_one(Session.sessionId == session_id[10:])
        if session:
            session.lastActivity = datetime.now(UTC)
            session.messageCount += 1
            await session.save()

    # -----------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------

    @staticmethod
    async def _get_session(session_id: str, user_id: str) -> Session:
        """Fetch by ObjectId first, then by sessionId."""
        session = None
        try:
            session = await Session.get(PydanticObjectId(session_id))
        except Exception:
            session = await Session.find_one(Session.sessionId == session_id)

        if not session or session.deleted_at:
            raise NotFound("session", session_id)
        if session.owner != user_id:
            raise Forbidden("session.not_owner", "Not the session owner")
        return session
