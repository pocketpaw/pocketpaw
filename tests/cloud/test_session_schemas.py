"""Tests for sessions domain schemas."""

from __future__ import annotations

from datetime import UTC, datetime

from ee.cloud.sessions.schemas import (
    CreateSessionRequest,
    SessionResponse,
    UpdateSessionRequest,
)


def test_create_session_defaults():
    req = CreateSessionRequest()
    assert req.title == "New Chat" and req.pocket_id is None


def test_create_session_with_pocket():
    req = CreateSessionRequest(title="Analysis", pocket_id="p123")
    assert req.pocket_id == "p123"


def test_create_session_all_fields():
    req = CreateSessionRequest(
        title="My Session",
        pocket_id="p1",
        group_id="g1",
        agent_id="a1",
    )
    assert req.title == "My Session"
    assert req.pocket_id == "p1"
    assert req.group_id == "g1"
    assert req.agent_id == "a1"


def test_update_session_all_optional():
    req = UpdateSessionRequest()
    assert req.title is None and req.pocket_id is None


def test_update_session_partial():
    req = UpdateSessionRequest(title="Renamed")
    assert req.title == "Renamed"
    assert req.pocket_id is None


def test_update_session_pocket_link():
    req = UpdateSessionRequest(pocket_id="p456")
    assert req.pocket_id == "p456"


def test_session_response():
    now = datetime.now(UTC)
    resp = SessionResponse(
        id="1",
        session_id="uuid-1",
        workspace="w1",
        owner="u1",
        title="Chat",
        pocket=None,
        group=None,
        agent=None,
        message_count=0,
        last_activity=now,
        created_at=now,
    )
    assert resp.session_id == "uuid-1"
    assert resp.deleted_at is None


def test_session_response_with_pocket():
    now = datetime.now(UTC)
    resp = SessionResponse(
        id="2",
        session_id="uuid-2",
        workspace="w1",
        owner="u1",
        title="Pocket Chat",
        pocket="p1",
        group="g1",
        agent="a1",
        message_count=5,
        last_activity=now,
        created_at=now,
    )
    assert resp.pocket == "p1"
    assert resp.group == "g1"
    assert resp.agent == "a1"
    assert resp.message_count == 5


def test_session_response_with_deleted_at():
    now = datetime.now(UTC)
    resp = SessionResponse(
        id="3",
        session_id="uuid-3",
        workspace="w1",
        owner="u1",
        title="Deleted Chat",
        pocket=None,
        group=None,
        agent=None,
        message_count=10,
        last_activity=now,
        created_at=now,
        deleted_at=now,
    )
    assert resp.deleted_at is not None
