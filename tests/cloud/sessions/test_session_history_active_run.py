"""``get_history`` surfaces ``active_run`` for the session's scope."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.sessions import service as sessions_service
from pocketpaw_ee.cloud.sessions.dto import CreateSessionRequest

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("mongo_db")]


def _ctx(user_id: str = "u1", workspace_id: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _spec(
    *,
    run_id: str,
    context_type: str,
    scope_id: str,
    workspace: str = "w1",
) -> RunSpec:
    return RunSpec(
        run_id=run_id,
        workspace_id=workspace,
        context_type=context_type,
        scope_id=scope_id,
        session_key=f"{context_type}:{scope_id}",
        group=scope_id if context_type in ("dm", "group") else None,
        user_id="u1",
        agent_id="agent-x",
        client_message_id=f"c-{run_id}",
        user_message_id="m1",
        content="hi",
        history=[],
        intent=None,
    )


async def test_session_history_includes_active_run_for_session_scope() -> None:
    session = await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="t"))
    assert session.context_type == "session"

    await run_service.create_run(_spec(run_id="live", context_type="session", scope_id=session.id))
    await run_service.mark_running("live")

    result = await sessions_service.get_history(session.id, "u1")

    assert result["active_run"] == {"run_id": "live", "status": "running"}


async def test_session_history_active_run_null_when_no_run() -> None:
    session = await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="t"))
    result = await sessions_service.get_history(session.id, "u1")
    assert result["active_run"] is None


async def test_session_history_active_run_null_when_run_completed() -> None:
    session = await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="t"))

    await run_service.create_run(_spec(run_id="done", context_type="session", scope_id=session.id))
    await run_service.mark_completed("done", assistant_message_id="m2", partial_text="ok")

    result = await sessions_service.get_history(session.id, "u1")
    assert result["active_run"] is None


async def test_session_history_active_run_for_group_scope() -> None:
    session = await sessions_service.create(
        _ctx(),
        "w1",
        CreateSessionRequest(title="t", group_id="g1", agent_id="agent-x"),
    )
    assert session.context_type == "group"

    await run_service.create_run(_spec(run_id="grp", context_type="group", scope_id="g1"))
    # Status stays ``queued`` — verifies non-terminal lookup matches both
    # ``queued`` and ``running``.

    result = await sessions_service.get_history(session.id, "u1")
    assert result["active_run"] == {"run_id": "grp", "status": "queued"}


async def test_session_history_active_run_for_pocket_scope() -> None:
    session = await sessions_service.create(
        _ctx(),
        "w1",
        CreateSessionRequest(title="t", pocket_id="p1", agent_id="agent-x"),
    )
    assert session.context_type == "pocket"

    await run_service.create_run(_spec(run_id="pk", context_type="pocket", scope_id="p1"))
    await run_service.mark_running("pk")

    result = await sessions_service.get_history(session.id, "u1")
    assert result["active_run"] == {"run_id": "pk", "status": "running"}


async def test_session_history_active_run_for_pocket_session_session_scope_run() -> None:
    """Regression: the desktop client POSTs pocket chats to
    ``/cloud/chat/session/{_id}/agent`` (not ``/cloud/chat/pocket/...``),
    so the run is written under ``(session, str(session.id))`` even though
    the Session doc has ``context_type=pocket``. The helper must still find
    it — otherwise no resume-on-refresh."""
    session = await sessions_service.create(
        _ctx(),
        "w1",
        CreateSessionRequest(title="t", pocket_id="p1", agent_id="agent-x"),
    )
    assert session.context_type == "pocket"

    # Run is in the session scope, not the pocket scope — matches the
    # frontend's actual POST URL pattern.
    await run_service.create_run(
        _spec(run_id="pk-as-session", context_type="session", scope_id=session.id)
    )
    await run_service.mark_running("pk-as-session")

    result = await sessions_service.get_history(session.id, "u1")
    assert result["active_run"] == {"run_id": "pk-as-session", "status": "running"}


async def test_session_history_active_run_isolated_by_workspace() -> None:
    """A run in a different workspace must NOT surface as active for this
    session — tenancy filter on the lookup."""
    session = await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="t"))
    # Same scope_id, but a different workspace owns the run.
    await run_service.create_run(
        _spec(
            run_id="other",
            context_type="session",
            scope_id=session.id,
            workspace="other-ws",
        )
    )
    await run_service.mark_running("other")

    result = await sessions_service.get_history(session.id, "u1")
    assert result["active_run"] is None
