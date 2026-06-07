"""MongoMemoryStore._load_session_index_async — shape + filtering contract.

Covers the API path behind ``GET /sessions/runtime`` on MongoDB-backed
deployments. The endpoint expects a dict compatible with the file store's
``_load_session_index`` so the router is backend-agnostic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud.models.session import Session

pytestmark = pytest.mark.asyncio


async def _make_session(
    *,
    session_id: str,
    title: str = "New Chat",
    context_type: str = "pocket",
    pocket: str | None = None,
    group: str | None = None,
    last_activity: datetime | None = None,
    message_count: int = 0,
    deleted_at: datetime | None = None,
    workspace: str = "ws-1",
    owner: str = "user-1",
) -> Session:
    doc = Session(
        sessionId=session_id,
        context_type=context_type,  # type: ignore[arg-type]
        pocket=pocket,
        group=group,
        workspace=workspace,
        owner=owner,
        title=title,
        lastActivity=last_activity or datetime.now(UTC),
        messageCount=message_count,
        deleted_at=deleted_at,
    )
    await doc.insert()
    return doc


class TestLoadSessionIndexAsync:
    async def test_returns_empty_when_no_sessions(self, store):
        index = await store._load_session_index_async()
        assert index == {}

    async def test_returns_entry_with_expected_shape(self, store):
        await _make_session(
            session_id="websocket_abc123",
            title="Hello world",
            last_activity=datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
            message_count=3,
        )
        index = await store._load_session_index_async()
        assert "websocket_abc123" in index
        entry = index["websocket_abc123"]
        assert entry == {
            "title": "Hello world",
            "channel": "websocket",
            "last_activity": "2026-04-10T12:00:00+00:00",
            "message_count": 3,
        }

    async def test_excludes_group_sessions(self, store):
        await _make_session(session_id="pocket-session", context_type="pocket", pocket="pocket-1")
        await _make_session(session_id="group-session", context_type="group", group="group-1")
        index = await store._load_session_index_async()
        assert "pocket-session" in index
        assert "group-session" not in index

    async def test_excludes_soft_deleted_sessions(self, store):
        await _make_session(session_id="alive")
        await _make_session(
            session_id="deleted",
            deleted_at=datetime.now(UTC) - timedelta(days=1),
        )
        index = await store._load_session_index_async()
        assert "alive" in index
        assert "deleted" not in index

    async def test_channel_fallback_for_keys_without_underscore(self, store):
        await _make_session(session_id="noprefix")
        index = await store._load_session_index_async()
        assert index["noprefix"]["channel"] == "unknown"

    async def test_empty_title_coerced_to_default(self, store):
        # Session model defaults title to "New Chat", so explicitly empty string
        # should still show something sensible in the index.
        await _make_session(session_id="websocket_x", title="")
        index = await store._load_session_index_async()
        assert index["websocket_x"]["title"] == "New Chat"
