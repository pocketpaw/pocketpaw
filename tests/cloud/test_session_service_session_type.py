"""Tests for free-floating 'session' context_type support in Session model + service."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.sessions import service as sessions_service
from pocketpaw_ee.cloud.sessions.dto import CreateSessionRequest


def _ctx(user_id: str = "u1", workspace_id: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_create_session_no_pocket_no_group_uses_session_type(mongo_db):
    """When neither pocket_id nor group_id is provided, context_type must be 'session'."""
    body = CreateSessionRequest(title="Free chat", agent_id="a1")
    s = await sessions_service.create(_ctx(), "w1", body)

    assert s.context_type == "session"
    assert s.pocket is None
    assert s.group is None
    assert s.agent == "a1"
    assert s.workspace == "w1"
    assert s.owner == "u1"


@pytest.mark.asyncio
async def test_create_session_with_pocket_id_keeps_pocket_type(mongo_db):
    """Backwards-compat: pocket_id still produces context_type='pocket'."""
    body = CreateSessionRequest(title="t", pocket_id="p1")
    s = await sessions_service.create(_ctx(), "w1", body)

    assert s.context_type == "pocket"
    assert s.pocket == "p1"


def test_session_model_accepts_session_context_type():
    """Session model validator allows context_type='session' with no pocket/group."""
    from pocketpaw_ee.cloud.models.session import Session as SessionDoc

    s = SessionDoc.model_construct(
        sessionId="ws_test",
        context_type="session",
        workspace="w1",
        owner="u1",
    )
    result = s._enforce_context()
    assert result.context_type == "session"
    assert result.pocket is None
    assert result.group is None


def test_session_model_session_type_rejects_pocket_field():
    """Session model validator rejects context_type='session' when pocket is set."""
    from pocketpaw_ee.cloud.models.session import Session as SessionDoc

    s = SessionDoc.model_construct(
        sessionId="ws_test",
        context_type="session",
        workspace="w1",
        owner="u1",
        pocket="p1",
    )
    with pytest.raises(ValueError):
        s._enforce_context()


def test_session_model_session_type_rejects_group_field():
    """Session model validator rejects context_type='session' when group is set."""
    from pocketpaw_ee.cloud.models.session import Session as SessionDoc

    s = SessionDoc.model_construct(
        sessionId="ws_test",
        context_type="session",
        workspace="w1",
        owner="u1",
        group="g1",
    )
    with pytest.raises(ValueError):
        s._enforce_context()
