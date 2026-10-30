"""Unit tests for ``_ensure_scope_session`` with SESSION kind."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.chat.agent_router import _ensure_scope_session
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind


def _ctx():
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="000000000000000000000001",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )


@pytest.mark.asyncio
async def test_ensure_scope_session_returns_existing_session_id():
    from pocketpaw_ee.cloud.models.session import Session

    fake = Session.model_construct(sessionId="websocket_abc123")
    with patch.object(Session, "get", AsyncMock(return_value=fake)):
        sid = await _ensure_scope_session(_ctx())
    assert sid == "websocket_abc123"


@pytest.mark.asyncio
async def test_ensure_scope_session_returns_none_when_missing():
    from pocketpaw_ee.cloud.models.session import Session

    with patch.object(Session, "get", AsyncMock(return_value=None)):
        sid = await _ensure_scope_session(_ctx())
    assert sid is None
