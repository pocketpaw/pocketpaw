"""MongoMemoryStore._load_session_index_async — shape + filtering contract.

Covers the API path behind ``GET /sessions/runtime`` on MongoDB-backed
deployments. The endpoint expects a dict compatible with the file store's
``_load_session_index`` so the router is backend-agnostic.

Updated 2026-08-01: the scope arguments became required. The tenant-filtering
assertions live in ``test_scoping`` at the bottom; the shape tests above pass
the seed defaults so they keep measuring shape and nothing else.
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
        index = await store._load_session_index_async(workspace_id="ws-1", owner_id="user-1")
        assert index == {}

    async def test_returns_entry_with_expected_shape(self, store):
        await _make_session(
            session_id="websocket_abc123",
            title="Hello world",
            last_activity=datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
            message_count=3,
        )
        index = await store._load_session_index_async(workspace_id="ws-1", owner_id="user-1")
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
        index = await store._load_session_index_async(workspace_id="ws-1", owner_id="user-1")
        assert "pocket-session" in index
        assert "group-session" not in index

    async def test_excludes_soft_deleted_sessions(self, store):
        await _make_session(session_id="alive")
        await _make_session(
            session_id="deleted",
            deleted_at=datetime.now(UTC) - timedelta(days=1),
        )
        index = await store._load_session_index_async(workspace_id="ws-1", owner_id="user-1")
        assert "alive" in index
        assert "deleted" not in index

    async def test_channel_fallback_for_keys_without_underscore(self, store):
        await _make_session(session_id="noprefix")
        index = await store._load_session_index_async(workspace_id="ws-1", owner_id="user-1")
        assert index["noprefix"]["channel"] == "unknown"

    async def test_empty_title_coerced_to_default(self, store):
        # Session model defaults title to "New Chat", so explicitly empty string
        # should still show something sensible in the index.
        await _make_session(session_id="websocket_x", title="")
        index = await store._load_session_index_async(workspace_id="ws-1", owner_id="user-1")
        assert index["websocket_x"]["title"] == "New Chat"


class TestScoping:
    """The tenant filter, which is half of what makes ``GET /sessions/runtime``
    correct — the route guard is the other half. This index backs a "your
    sessions" listing, so a read that authenticated the caller but did not
    scope the query would still answer with rows belonging to someone else.
    """

    async def test_another_workspaces_sessions_are_excluded(self, store):
        await _make_session(session_id="mine", workspace="ws-1", owner="user-1")
        await _make_session(session_id="theirs", workspace="ws-2", owner="user-2")
        index = await store._load_session_index_async(workspace_id="ws-1", owner_id="user-1")
        assert set(index) == {"mine"}

    async def test_another_owner_in_the_SAME_workspace_is_excluded(self, store):
        # Workspace alone is not enough — this index backs a "your sessions"
        # listing, and a colleague's private chat titles are not the caller's.
        await _make_session(session_id="mine", workspace="ws-1", owner="user-1")
        await _make_session(session_id="colleague", workspace="ws-1", owner="user-2")
        index = await store._load_session_index_async(workspace_id="ws-1", owner_id="user-1")
        assert set(index) == {"mine"}

    async def test_the_scope_arguments_are_not_optional(self, store):
        # Required rather than defaulting to None, so the tenant-wide version
        # cannot come back by omission: forgetting the scope fails to call.
        await _make_session(session_id="mine")
        with pytest.raises(TypeError):
            await store._load_session_index_async()
