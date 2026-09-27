# tests/cloud/sessions/test_list_by_agents.py — POST /sessions/by-agents and
# ``sessions_service.list_by_agents``, the batch form of ``list_by_agent``.
# Created 2026-09-27 (feat/bulk-grants-conversations): every requested agent id
# is a key (empty list when none); rows are the caller's own, in the active
# workspace, not soft-deleted, newest activity first; each agent's list equals
# what ``list_by_agent`` returns; all of it comes from ONE Mongo query; the
# route validates 1..100 ids and dedupes them.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.models.session import Session
from pocketpaw_ee.cloud.sessions import service as sessions_service
from pocketpaw_ee.cloud.sessions.dto import session_to_wire_dict

pytestmark = pytest.mark.usefixtures("mongo_db")

_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


async def _mk(sid: str, *, agent: str | None, owner: str = "u1", ws: str = "w1", **ov) -> Session:
    doc = Session(
        sessionId=sid,
        context_type="session",
        workspace=ws,
        owner=owner,
        agent=agent,
        title=sid,
        **ov,
    )
    await doc.insert()
    return doc


async def _seed() -> None:
    await _mk("a-old", agent="agent-A", lastActivity=_NOW - timedelta(hours=2))
    await _mk("a-new", agent="agent-A", lastActivity=_NOW)
    await _mk("b-1", agent="agent-B", lastActivity=_NOW - timedelta(hours=1))
    await _mk("a-deleted", agent="agent-A", deleted_at=_NOW)
    await _mk("a-other-owner", agent="agent-A", owner="u2")
    await _mk("a-other-ws", agent="agent-A", ws="w2")
    await _mk("c-1", agent="agent-C")  # not requested
    await _mk("no-agent", agent=None)


async def test_every_requested_agent_is_a_key_with_scoped_sorted_rows() -> None:
    await _seed()
    ctx = sessions_service.legacy_ctx("u1", "w1")
    grouped = await sessions_service.list_by_agents(ctx, "w1", ["agent-B", "agent-A", "agent-Z"])

    assert list(grouped) == ["agent-B", "agent-A", "agent-Z"]
    assert [s.sessionId for s in grouped["agent-A"]] == ["a-new", "a-old"]
    assert [s.sessionId for s in grouped["agent-B"]] == ["b-1"]
    assert grouped["agent-Z"] == []


async def test_matches_list_by_agent_per_agent() -> None:
    await _seed()
    ctx = sessions_service.legacy_ctx("u1", "w1")
    grouped = await sessions_service.list_by_agents(ctx, "w1", ["agent-A", "agent-B"])
    for agent_id in ("agent-A", "agent-B"):
        single = await sessions_service.list_by_agent(ctx, "w1", agent_id)
        assert [session_to_wire_dict(s) for s in grouped[agent_id]] == [
            session_to_wire_dict(s) for s in single
        ]


async def test_uses_a_single_query(monkeypatch) -> None:
    await _seed()
    real = Session.find
    calls: list[tuple] = []

    def _counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(Session, "find", _counting)
    ctx = sessions_service.legacy_ctx("u1", "w1")
    grouped = await sessions_service.list_by_agents(ctx, "w1", ["agent-A", "agent-B", "agent-Z"])
    assert len(calls) == 1
    assert sum(len(v) for v in grouped.values()) == 3


def _fake_user(role: str = "member", workspace_id: str = "w1", user_id: str = "u1"):
    return SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role=role)],
    )


def _app(user_id: str = "u1", workspace_id: str = "w1") -> FastAPI:
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.sessions.router import router
    from pocketpaw_ee.cloud.shared.deps import current_user_id

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: _fake_user(
        workspace_id=workspace_id, user_id=user_id
    )
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[current_user_id] = lambda: user_id
    return app


async def _post(app: FastAPI, body: dict):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/api/v1/sessions/by-agents", json=body)


async def test_route_returns_every_key_scoped_to_the_caller() -> None:
    await _seed()
    res = await _post(_app(), {"agent_ids": ["agent-A", "agent-Z", "agent-A"]})
    assert res.status_code == 200, res.text
    sessions = res.json()["sessions"]
    assert list(sessions) == ["agent-A", "agent-Z"]
    assert [s["sessionId"] for s in sessions["agent-A"]] == ["a-new", "a-old"]
    assert sessions["agent-Z"] == []

    # Another user in the same workspace only ever sees their own row.
    other = (await _post(_app(user_id="u2"), {"agent_ids": ["agent-A"]})).json()
    assert [s["sessionId"] for s in other["sessions"]["agent-A"]] == ["a-other-owner"]

    # The same user in another workspace sees that workspace's row only.
    ws2 = (await _post(_app(workspace_id="w2"), {"agent_ids": ["agent-A"]})).json()
    assert [s["sessionId"] for s in ws2["sessions"]["agent-A"]] == ["a-other-ws"]


@pytest.mark.parametrize("ids", [[], [f"a{i}" for i in range(101)]])
async def test_route_bounds_are_422(ids) -> None:
    res = await _post(_app(), {"agent_ids": ids})
    assert res.status_code == 422
