# tests/cloud/sessions/test_service.py — list_for_user helper.
#
# Created: 2026-05-24 — Tests for the per-user sessions listing the
# chat surface preamble calls (``_session_count``). Three guarantees:
#   1. Owner filter — only the requested user's sessions come back when
#      multiple users share a workspace.
#   2. Workspace filter — only the requested workspace's sessions come
#      back when the same user has sessions in multiple workspaces.
#   3. Limit cap — when ``limit`` is set, the result honors it.
#
# Sibling sessions-service tests (CRUD, touch, list_for_owner, etc.)
# live in ``test_service_v2.py`` — the v2 suffix is a transitional name
# from the 2026-04-27 cloud rewrite. This file is the canonical home
# going forward and the natural place to add new ``service.py`` tests.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
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


async def test_list_for_user_returns_only_owner_sessions() -> None:
    """Two users in the same workspace — only the requested owner's rows."""
    await sessions_service.create(_ctx("u1"), "w1", CreateSessionRequest(title="u1-a"))
    await sessions_service.create(_ctx("u1"), "w1", CreateSessionRequest(title="u1-b"))
    await sessions_service.create(_ctx("u2"), "w1", CreateSessionRequest(title="u2-only"))

    rows = await sessions_service.list_for_user("w1", "u1")

    assert len(rows) == 2
    titles = {r["title"] for r in rows}
    assert titles == {"u1-a", "u1-b"}
    assert all(r["owner"] == "u1" for r in rows)


async def test_list_for_user_respects_workspace_boundary() -> None:
    """Same user with sessions in two workspaces — only the asked one."""
    await sessions_service.create(_ctx("u1"), "w1", CreateSessionRequest(title="in-w1"))
    await sessions_service.create(_ctx("u1"), "w2", CreateSessionRequest(title="in-w2"))

    rows = await sessions_service.list_for_user("w1", "u1")

    assert len(rows) == 1
    assert rows[0]["title"] == "in-w1"
    assert rows[0]["workspace"] == "w1"


async def test_list_for_user_limit() -> None:
    """``limit`` caps the result size."""
    for i in range(5):
        await sessions_service.create(_ctx("u1"), "w1", CreateSessionRequest(title=f"s{i}"))

    rows = await sessions_service.list_for_user("w1", "u1", limit=2)

    assert len(rows) == 2
