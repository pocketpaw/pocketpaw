"""Session.messageCount + lastActivity are maintained by MongoMemoryStore.

Before this was moved into the store, metadata upkeep lived in a parallel
``chat_persistence`` bus subscriber that was deleted once
``MongoMemoryStore.save`` became the single write path. Sessions appeared
frozen in the sidebar because no one was touching them. These tests pin
the upkeep into the store's contract so any future refactor that skips
the touch lights up here first.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.session import Session
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership

from pocketpaw.memory.protocol import MemoryEntry, MemoryType

pytestmark = pytest.mark.asyncio


def _entry(session_key: str, content: str, role: str = "user") -> MemoryEntry:
    return MemoryEntry(
        id="",
        type=MemoryType.SESSION,
        content=content,
        role=role,
        session_key=session_key,
    )


async def _user_with_ws(workspace_id: str = "ws-1") -> str:
    user = User(
        email="up@example.com",
        hashed_password="x",
        is_active=True,
        is_verified=True,
        name="up",
        workspaces=[WorkspaceMembership(workspace=workspace_id, role="owner")],
    )
    await user.insert()
    return str(user.id)


class TestSessionUpkeep:
    async def test_save_touches_existing_session(self, store) -> None:
        owner = await _user_with_ws()
        session = Session(
            sessionId="websocket_upk-1",
            context_type="pocket",
            workspace="ws-1",
            owner=owner,
            title="Chat",
        )
        await session.insert()
        assert session.messageCount == 0
        prior_activity = session.lastActivity

        await store.save(_entry("websocket:upk-1", "first"))
        await store.save(_entry("websocket:upk-1", "second"))

        reloaded = await Session.find_one(Session.sessionId == "websocket_upk-1")
        assert reloaded is not None
        assert reloaded.messageCount == 2
        assert reloaded.lastActivity != prior_activity

    async def test_save_auto_creates_pocket_session_when_missing(self, store) -> None:
        """`/chat/stream` skips POST /sessions on first turn — the store
        should create the Session row so the sidebar picks it up."""
        await _user_with_ws()

        await store.save(_entry("websocket:new-1", "hello"))

        session = await Session.find_one(Session.sessionId == "websocket_new-1")
        assert session is not None
        assert session.context_type == "pocket"
        assert session.workspace == "ws-1"
        assert session.messageCount == 1

    async def test_save_without_any_user_still_persists_message(self, store) -> None:
        """Fresh install (no users) — message row survives even though no
        Session could be auto-created."""
        from pocketpaw_ee.cloud.models.message import Message

        await store.save(_entry("websocket:lonely", "hi"))

        msg = await Message.find_one(Message.session_key == "websocket_lonely")
        assert msg is not None
        assert msg.content == "hi"

        session = await Session.find_one(Session.sessionId == "websocket_lonely")
        assert session is None
