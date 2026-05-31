"""Regression: ``get_history`` must match session-scope writes by ``session_key``
prefix so messages still surface when the ``Session.agent`` backfill silently
fails.

Bug recap (parent diagnosis): the SSE stream writes messages keyed by
``cloud:session:{session_id}:{target_agent_id}``, but the read side in
``sessions/service.get_history`` queries
``cloud:session:{session_id}:{session.agent}``. When ``_ensure_scope_session``
fails to backfill ``Session.agent`` on a freshly-created session-scope row, the
stored field stays ``None`` and the read returns 0 rows — the user sees their
optimistically-pushed message but no agent reply, even though the agent did
respond and persist the assistant message.

The fix uses a regex/prefix match: any row whose ``session_key`` starts with
``cloud:session:{session_id}:`` belongs to this session, regardless of which
``agent`` value the writer used at the time.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.chat import message_service
from pocketpaw_ee.cloud.sessions import service as sessions_service
from pocketpaw_ee.cloud.sessions.dto import CreateSessionRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(user_id: str = "u1", workspace_id: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def test_get_history_returns_messages_when_session_agent_is_null() -> None:
    """The bug: a session row with ``agent=None`` cannot read its own history.

    The writer (SSE stream) used the live ``target_agent_id`` to build
    ``session_key=cloud:session:{sid}:agent-x``. If the ``Session.agent``
    backfill failed (broad ``except`` swallowed it), the read query becomes
    ``cloud:session:{sid}:None`` and matches zero rows.

    With the prefix-match fix, both the user message and the assistant reply
    must come back regardless of the stored ``Session.agent`` value.
    """
    # Create a session-scope row with no agent attached — exactly the state
    # left behind when ``_ensure_scope_session`` silently fails its save.
    session = await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="t"))
    assert session.context_type == "session"
    assert session.agent is None  # the precondition the bug depends on

    # The SSE stream wrote messages with the live ``target_agent_id``. Mirror
    # the exact key the writer would have produced.
    session_key = f"cloud:session:{session.id}:agent-x"

    await message_service.persist_user_message_for_scope(
        kind="session",
        scope_id=session.id,
        user_id="u1",
        workspace_id="w1",
        session_key=session_key,
        content="what's the weather?",
    )
    await message_service.persist_assistant_message_for_scope(
        kind="session",
        scope_id=session.id,
        user_id="u1",
        workspace_id="w1",
        session_key=session_key,
        target_agent_id="agent-x",
        content="sunny and 70F",
    )

    history = await sessions_service.get_history(session.id, "u1")

    contents = [m["content"] for m in history["messages"]]
    assert "what's the weather?" in contents, (
        "user message must surface even when Session.agent is None"
    )
    assert "sunny and 70F" in contents, (
        "assistant reply must surface even when Session.agent is None"
    )


async def test_get_history_with_backfilled_agent_still_works() -> None:
    """The fix must be backwards-compatible: sessions with a stored ``agent``
    keep returning the same history. We backfill ``Session.agent`` post-hoc
    and confirm the row count is unchanged."""
    session = await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="t", agent_id="agent-x")
    )
    session_key = f"cloud:session:{session.id}:agent-x"

    await message_service.persist_user_message_for_scope(
        kind="session",
        scope_id=session.id,
        user_id="u1",
        workspace_id="w1",
        session_key=session_key,
        content="hi",
    )
    await message_service.persist_assistant_message_for_scope(
        kind="session",
        scope_id=session.id,
        user_id="u1",
        workspace_id="w1",
        session_key=session_key,
        target_agent_id="agent-x",
        content="hi back",
    )

    history = await sessions_service.get_history(session.id, "u1")
    assert len(history["messages"]) == 2
