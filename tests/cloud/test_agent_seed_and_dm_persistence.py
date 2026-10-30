"""Tests for the pocketpaw agent seed + DM chat persistence with attachments.

Covers:
- ``seed_default_agent`` is idempotent and creates the expected Agent.
- ``MongoMemoryStore.save`` persists attachments on the Message doc and
  touches the linked Session (messageCount / lastActivity).
- ``SessionService.get_history`` returns attachments per message.
- ``SessionService.list_by_agent`` filters to sessions for a given agent.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def beanie_db():
    """Isolated Beanie session for these tests (all EE documents registered)."""
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    db_name = f"test_agent_seed_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]

    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]

    await init_beanie(database=db, document_models=list(ALL_DOCUMENTS))
    yield db


class TestSeedDefaultAgent:
    async def test_creates_agent_with_pocketpaw_slug(self, beanie_db) -> None:
        from pocketpaw_ee.cloud.auth.core import seed_default_agent
        from pocketpaw_ee.cloud.models.agent import Agent

        agent, created = await seed_default_agent(workspace_id="ws-1", owner_id="user-1")
        assert agent is not None
        assert created is True
        assert agent.slug == "pocketpaw"
        assert agent.workspace == "ws-1"
        assert agent.owner == "user-1"

        # Confirm persisted
        found = await Agent.find_one(Agent.workspace == "ws-1", Agent.slug == "pocketpaw")
        assert found is not None

    async def test_is_idempotent(self, beanie_db) -> None:
        from pocketpaw_ee.cloud.auth.core import seed_default_agent
        from pocketpaw_ee.cloud.models.agent import Agent

        first, first_created = await seed_default_agent("ws-1", "user-1")
        second, second_created = await seed_default_agent("ws-1", "user-1")
        assert first is not None and second is not None
        assert first.id == second.id
        # First call creates; second call finds existing — accurate counter.
        assert first_created is True
        assert second_created is False

        # Still only one agent for this workspace.
        count = await Agent.find(Agent.workspace == "ws-1", Agent.slug == "pocketpaw").count()
        assert count == 1

    async def test_per_workspace_agents(self, beanie_db) -> None:
        from pocketpaw_ee.cloud.auth.core import seed_default_agent

        a1, _ = await seed_default_agent("ws-1", "user-1")
        a2, _ = await seed_default_agent("ws-2", "user-1")
        assert a1 is not None and a2 is not None
        assert a1.id != a2.id
        assert a1.workspace != a2.workspace


class TestEnsureDefaultAgentBackfill:
    async def test_backfills_existing_workspaces(self, beanie_db) -> None:
        from pocketpaw_ee.cloud.auth.core import ensure_default_agent_all_workspaces
        from pocketpaw_ee.cloud.models.agent import Agent
        from pocketpaw_ee.cloud.models.workspace import Workspace, WorkspaceSettings

        ws1 = Workspace(name="A", slug="a", owner="u-1", settings=WorkspaceSettings())
        ws2 = Workspace(name="B", slug="b", owner="u-2", settings=WorkspaceSettings())
        await ws1.insert()
        await ws2.insert()

        seeded = await ensure_default_agent_all_workspaces()
        assert seeded == 2

        a1 = await Agent.find_one(Agent.workspace == str(ws1.id), Agent.slug == "pocketpaw")
        a2 = await Agent.find_one(Agent.workspace == str(ws2.id), Agent.slug == "pocketpaw")
        assert a1 is not None and a2 is not None
        assert a1.owner == "u-1"
        assert a2.owner == "u-2"

    async def test_backfill_is_idempotent(self, beanie_db) -> None:
        from pocketpaw_ee.cloud.auth.core import ensure_default_agent_all_workspaces
        from pocketpaw_ee.cloud.models.agent import Agent
        from pocketpaw_ee.cloud.models.workspace import Workspace, WorkspaceSettings

        ws = Workspace(name="A", slug="a", owner="u-1", settings=WorkspaceSettings())
        await ws.insert()

        first = await ensure_default_agent_all_workspaces()
        second = await ensure_default_agent_all_workspaces()

        # First boot creates one agent; second boot reports zero actually
        # created — the old implementation counted existing rows as "seeded"
        # which made the log line lie on every restart.
        assert first == 1
        assert second == 0

        count = await Agent.find(Agent.workspace == str(ws.id), Agent.slug == "pocketpaw").count()
        assert count == 1


class TestMongoMemoryStoreAttachments:
    """The canonical user-message write path. Attachments arrive via
    ``entry.metadata["attachments"]`` (piped through InboundMessage → agent
    loop → memory), and MongoMemoryStore persists them on the same row as
    the content — not a second row."""

    async def _session(self, workspace_id: str = "ws-1") -> str:
        from pocketpaw_ee.cloud.models.session import Session
        from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership

        user = User(
            email="m@example.com",
            hashed_password="x",
            is_active=True,
            is_verified=True,
            name="m",
            workspaces=[WorkspaceMembership(workspace=workspace_id, role="owner")],
        )
        await user.insert()
        session = Session(
            sessionId="websocket_mm-1",
            context_type="pocket",
            workspace=workspace_id,
            owner=str(user.id),
            title="Chat",
        )
        await session.insert()
        return str(user.id)

    async def test_attachments_from_metadata_persist_on_single_row(self, beanie_db) -> None:
        from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore
        from pocketpaw_ee.cloud.models.message import Message

        from pocketpaw.memory.protocol import MemoryEntry, MemoryType

        await self._session()

        store = MongoMemoryStore()
        entry = MemoryEntry(
            id="",
            type=MemoryType.SESSION,
            content="What do you see in this image?",
            role="user",
            session_key="websocket:mm-1",
            metadata={
                "attachments": [
                    {
                        "type": "image",
                        "url": "/api/v1/uploads/abc",
                        "name": "chart.png",
                        "meta": {"mime": "image/png", "size": 137747, "id": "abc"},
                    }
                ]
            },
        )
        await store.save(entry)

        rows = await Message.find(
            Message.context_type == "pocket",
            Message.session_key == "websocket_mm-1",
        ).to_list()
        assert len(rows) == 1, f"expected a single message row, got {len(rows)}"
        [msg] = rows
        assert msg.content == "What do you see in this image?"
        assert len(msg.attachments) == 1
        att = msg.attachments[0]
        assert att.type == "image"
        assert att.url == "/api/v1/uploads/abc"
        assert att.name == "chart.png"
        assert att.meta["size"] == 137747

    async def test_no_attachments_key_yields_empty_list(self, beanie_db) -> None:
        from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore
        from pocketpaw_ee.cloud.models.message import Message

        from pocketpaw.memory.protocol import MemoryEntry, MemoryType

        await self._session()

        store = MongoMemoryStore()
        entry = MemoryEntry(
            id="",
            type=MemoryType.SESSION,
            content="just text",
            role="user",
            session_key="websocket:mm-1",
            metadata={},
        )
        await store.save(entry)

        msg = await Message.find_one(Message.session_key == "websocket_mm-1")
        assert msg is not None
        assert msg.attachments == []


class TestHistoryReturnsAttachments:
    async def test_pocket_history_exposes_attachments(self, beanie_db) -> None:
        from pocketpaw_ee.cloud.models.message import Attachment, Message
        from pocketpaw_ee.cloud.models.session import Session
        from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        user = User(
            email="u2@example.com",
            hashed_password="x",
            is_active=True,
            is_verified=True,
            is_superuser=False,
            name="u2",
            workspaces=[WorkspaceMembership(workspace="ws-1", role="member")],
        )
        await user.insert()
        session = Session(
            sessionId="hist-1",
            context_type="pocket",
            workspace="ws-1",
            owner=str(user.id),
            title="Chat",
        )
        await session.insert()

        msg = Message(
            context_type="pocket",
            session_key="hist-1",
            role="user",
            sender=str(user.id),
            sender_type="user",
            content="hi",
            attachments=[
                Attachment(
                    type="image",
                    url="/api/v1/uploads/xyz",
                    name="pic.png",
                    meta={"mime": "image/png", "size": 100},
                )
            ],
        )
        await msg.insert()

        result = await sessions_service.get_history("hist-1", str(user.id))
        messages = result["messages"]
        assert len(messages) == 1
        assert "attachments" in messages[0]
        assert len(messages[0]["attachments"]) == 1
        assert messages[0]["attachments"][0]["url"] == "/api/v1/uploads/xyz"
        assert messages[0]["attachments"][0]["name"] == "pic.png"


class TestListByAgent:
    async def test_filters_sessions_to_given_agent(self, beanie_db) -> None:
        from pocketpaw_ee.cloud.models.session import Session
        from pocketpaw_ee.cloud.sessions import service as sessions_service
        from pocketpaw_ee.cloud.sessions.dto import session_to_wire_dict

        s_match = Session(
            sessionId="s1",
            context_type="pocket",
            workspace="ws-1",
            owner="user-1",
            agent="agent-A",
            title="match",
        )
        s_other = Session(
            sessionId="s2",
            context_type="pocket",
            workspace="ws-1",
            owner="user-1",
            agent="agent-B",
            title="other",
        )
        s_no_agent = Session(
            sessionId="s3",
            context_type="pocket",
            workspace="ws-1",
            owner="user-1",
            title="no agent",
        )
        for s in (s_match, s_other, s_no_agent):
            await s.insert()

        ctx = sessions_service.legacy_ctx("user-1", "ws-1")
        items = await sessions_service.list_by_agent(ctx, "ws-1", "agent-A")
        found = [session_to_wire_dict(s) for s in items]
        assert len(found) == 1
        assert found[0]["sessionId"] == "s1"
        assert found[0]["agent"] == "agent-A"

    async def test_respects_soft_delete(self, beanie_db) -> None:
        from datetime import UTC, datetime

        from pocketpaw_ee.cloud.models.session import Session
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        deleted = Session(
            sessionId="s-dead",
            context_type="pocket",
            workspace="ws-1",
            owner="user-1",
            agent="agent-A",
            title="deleted",
            deleted_at=datetime.now(UTC),
        )
        await deleted.insert()

        ctx = sessions_service.legacy_ctx("user-1", "ws-1")
        items = await sessions_service.list_by_agent(ctx, "ws-1", "agent-A")
        assert items == []
