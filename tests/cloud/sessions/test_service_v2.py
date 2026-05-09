"""Tests for the sessions service.

Uses the shared ``mongo_db`` fixture (mongomock-motor) so service
functions exercise real Beanie writes against an isolated in-memory DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.realtime.events import (
    SessionCreated,
    SessionDeleted,
    SessionUpdated,
)
from ee.cloud.sessions import service as sessions_service
from ee.cloud.sessions.dto import CreateSessionRequest, UpdateSessionRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(user_id: str = "u1", workspace_id: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


@pytest.fixture
def captured_legacy_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    class _FakeBus:
        async def emit(self, name: str, payload: dict) -> None:
            events.append((name, payload))

    monkeypatch.setattr("ee.cloud.sessions.service.event_bus", _FakeBus())
    return events


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def test_create_emits_session_created(recording_bus, captured_legacy_events) -> None:
    s = await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="My chat"))
    assert s.context_type == "session"
    assert s.title == "My chat"
    assert any(isinstance(e, SessionCreated) for e in recording_bus.events)
    assert any(name == "session.created" for (name, _) in captured_legacy_events)


async def test_create_with_pocket_id_uses_pocket_context() -> None:
    s = await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="t", pocket_id="p1"))
    assert s.context_type == "pocket"
    assert s.pocket == "p1"


async def test_create_with_existing_session_id_returns_existing() -> None:
    seeded = await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="orig", session_id="known-sid")
    )
    s2 = await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="orig", session_id="known-sid")
    )
    assert s2.id == seeded.id


async def test_create_with_existing_session_id_updates_pocket(recording_bus) -> None:
    await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="t", session_id="known"))
    recording_bus.events.clear()
    s2 = await sessions_service.create(
        _ctx(),
        "w1",
        CreateSessionRequest(title="t", session_id="known", pocket_id="p1"),
    )
    assert s2.pocket == "p1"
    assert any(isinstance(e, SessionUpdated) for e in recording_bus.events)


async def test_list_for_owner_returns_owners_only() -> None:
    await sessions_service.create(_ctx("u1"), "w1", CreateSessionRequest(title="a"))
    await sessions_service.create(_ctx("u2"), "w1", CreateSessionRequest(title="b"))
    items = await sessions_service.list_for_owner(_ctx("u1"), "w1")
    assert len(items) == 1
    assert items[0].title == "a"


async def test_update_emits_session_updated(recording_bus) -> None:
    s = await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="orig"))
    recording_bus.events.clear()
    updated = await sessions_service.update(_ctx(), s.id, UpdateSessionRequest(title="new"))
    assert updated.title == "new"
    assert any(isinstance(e, SessionUpdated) for e in recording_bus.events)


async def test_delete_emits_session_deleted(recording_bus) -> None:
    s = await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="t"))
    recording_bus.events.clear()
    await sessions_service.delete(_ctx(), s.id)
    assert any(isinstance(e, SessionDeleted) for e in recording_bus.events)


async def test_get_rejects_other_owner() -> None:
    from ee.cloud.shared.errors import Forbidden

    s = await sessions_service.create(_ctx("u1"), "w1", CreateSessionRequest(title="t"))
    with pytest.raises(Forbidden):
        await sessions_service.get(_ctx("u2"), s.id)


async def test_list_for_pocket_filters() -> None:
    await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="a", pocket_id="p1"))
    await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="b", pocket_id="p2"))
    p1_sessions = await sessions_service.list_for_pocket(_ctx(), "p1")
    assert len(p1_sessions) == 1
    assert p1_sessions[0].pocket == "p1"


async def test_link_pocket_sets_pocket_field() -> None:
    s = await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="t", session_id="my-sid")
    )
    await sessions_service.link_pocket("w1", "my-sid", "p1")
    refreshed = await sessions_service.get(_ctx(), s.id)
    assert refreshed.pocket == "p1"


async def test_link_pocket_noop_for_other_workspace() -> None:
    s = await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="t", session_id="my-sid")
    )
    await sessions_service.link_pocket("w_OTHER", "my-sid", "p1")
    refreshed = await sessions_service.get(_ctx(), s.id)
    assert refreshed.pocket is None


# ---------------------------------------------------------------------------
# touch — kept on Beanie. Patches the _SessionDoc module attr so we don't
# rely on real Mongo behavior for this hot-path emit assertion.
# ---------------------------------------------------------------------------


async def test_touch_emits_session_updated(recording_bus) -> None:
    from types import SimpleNamespace

    session = SimpleNamespace(
        id="s_oid",
        sessionId="websocket_abc",
        owner="u1",
        lastActivity=datetime.now(UTC),
        messageCount=0,
        save=AsyncMock(),
    )

    doc_stub = MagicMock()
    doc_stub.find_one = AsyncMock(return_value=session)

    with patch("ee.cloud.sessions.service._SessionDoc", new=doc_stub):
        await sessions_service.touch("websocket_abc")

    events = [e for e in recording_bus.events if isinstance(e, SessionUpdated)]
    assert len(events) == 1
    data = events[0].data
    assert data["session_id"] == "s_oid"
    assert data["user_id"] == "u1"
    assert isinstance(data["last_message_at"], str)
    session.save.assert_awaited_once()
    assert session.messageCount == 1


async def test_touch_no_emit_when_session_missing(recording_bus) -> None:
    doc_stub = MagicMock()
    doc_stub.find_one = AsyncMock(return_value=None)

    with patch("ee.cloud.sessions.service._SessionDoc", new=doc_stub):
        await sessions_service.touch("missing")

    assert not [e for e in recording_bus.events if isinstance(e, SessionUpdated)]
