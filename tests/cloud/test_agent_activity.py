# tests/cloud/test_agent_activity.py — workspace agent-activity board (HR-12a).
#
# Created: 2026-07-28 (feat/cockpit-agent-activity) — pins the read-only board
# end-to-end against REAL Beanie queries (mongomock-motor), because the property
# that matters most here is a QUERY property: every read is filtered to the
# caller's workspace. A Protocol fake in front of the service would assert the
# fold and prove nothing about the filter, so runs are inserted as real
# ChatRunDoc rows and the real ``chat.runs.service`` reads them back.
#
# Coverage:
#   * service.build_activity — response shape; the four status mappings
#     (active / blocked / idle / active-beats-a-failed-history); active_runs
#     counting with no double-count across the two overlapping reads; the
#     recency window edge; empty state.
#   * CROSS-WORKSPACE ISOLATION — at the service AND over HTTP, including a
#     caller who is a member of both workspaces.
#   * router — the wire shape, ordering, the rejected workspace_id query param,
#     and the auth gate (member 200 / unauthenticated 401) via the REAL RBAC
#     guard and the REAL current_workspace_id dependency.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pocketpaw_ee.cloud.agent_activity import service  # noqa: E402

from pocketpaw.mission_control.models import AgentStatus  # noqa: E402

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)


async def _run(
    *,
    workspace: str,
    agent_id: str,
    status: str,
    run_id: str | None = None,
    minutes_ago: int = 5,
    ended: bool = True,
) -> str:
    """Insert one ChatRunDoc. Returns its run_id.

    Inserted directly (not through ``create_run``) so a test can express a
    terminal run in one line; the READ path under test is the real service.
    """
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    rid = run_id or f"r-{workspace}-{agent_id}-{status}-{minutes_ago}"
    created = NOW - timedelta(minutes=minutes_ago)
    terminal = status not in ("queued", "running")
    doc = ChatRunDoc(
        run_id=rid,
        workspace=workspace,
        context_type="dm",
        scope_id=f"scope-{agent_id}",
        session_key=f"sess-{agent_id}",
        user_id="u1",
        agent_id=agent_id,
        client_message_id=f"cm-{rid}",
        user_message_id=f"um-{rid}",
        status=status,  # type: ignore[arg-type]
        createdAt=created,
        started_at=None if status == "queued" else created,
        ended_at=created if (terminal and ended) else None,
    )
    await doc.insert()
    return rid


async def _build(workspace: str = "w1"):
    return await service.build_activity(workspace, now=NOW)


# ===========================================================================
# Shape + status mapping
# ===========================================================================


async def test_board_shape(mongo_db):  # noqa: ARG001 — fixture initializes Beanie
    """Every entry carries the full wire shape, and ts stamps the build."""
    rid = await _run(workspace="w1", agent_id="scout", status="completed", minutes_ago=3)

    board = await _build()

    assert board.ts == NOW.isoformat()
    assert len(board.agents) == 1
    entry = board.agents[0]
    assert entry.agent_id == "scout"
    assert entry.status == AgentStatus.IDLE.value
    assert entry.active_runs == 0
    assert entry.last_run_id == rid
    assert entry.last_active == (NOW - timedelta(minutes=3)).isoformat()


@pytest.mark.parametrize(
    "status,expected",
    [
        ("queued", AgentStatus.ACTIVE),
        ("running", AgentStatus.ACTIVE),
        ("failed", AgentStatus.BLOCKED),
        ("interrupted", AgentStatus.BLOCKED),
        ("completed", AgentStatus.IDLE),
        # A user who stopped their own turn did not block the agent.
        ("cancelled", AgentStatus.IDLE),
    ],
)
async def test_status_mapping(mongo_db, status, expected):  # noqa: ARG001
    await _run(workspace="w1", agent_id="a1", status=status)

    board = await _build()

    assert [a.status for a in board.agents] == [expected.value]


async def test_active_beats_a_failed_history(mongo_db):  # noqa: ARG001
    """An agent with work in flight is ACTIVE even when its newest run failed.

    Ordering matters: the failed run is the NEWER of the two, so a naive
    "latest run decides" would report BLOCKED for an agent that is working.
    """
    await _run(workspace="w1", agent_id="a1", status="running", minutes_ago=30)
    await _run(workspace="w1", agent_id="a1", status="failed", minutes_ago=2)

    board = await _build()

    entry = board.agents[0]
    assert entry.status == AgentStatus.ACTIVE.value
    assert entry.active_runs == 1


async def test_active_runs_counts_each_live_run_once(mongo_db):  # noqa: ARG001
    """The active and recent reads overlap; a live run must not count twice."""
    await _run(workspace="w1", agent_id="a1", status="running", minutes_ago=9)
    await _run(workspace="w1", agent_id="a1", status="queued", minutes_ago=4)
    await _run(workspace="w1", agent_id="a1", status="completed", minutes_ago=40)

    board = await _build()

    assert board.agents[0].active_runs == 2


async def test_last_active_uses_start_for_a_live_run(mongo_db):  # noqa: ARG001
    """A running turn has no end yet, so it reports when it started."""
    await _run(workspace="w1", agent_id="a1", status="running", minutes_ago=15)

    board = await _build()

    assert board.agents[0].last_active == (NOW - timedelta(minutes=15)).isoformat()


# ===========================================================================
# Window + empty state
# ===========================================================================


async def test_empty_state(mongo_db):  # noqa: ARG001
    board = await _build()

    assert board.agents == []
    assert board.ts == NOW.isoformat()


async def test_agent_outside_the_window_is_omitted(mongo_db):  # noqa: ARG001
    """Nothing recent -> the agent is absent, not reported OFFLINE."""
    await _run(workspace="w1", agent_id="stale", status="completed", minutes_ago=60 * 25)
    await _run(workspace="w1", agent_id="fresh", status="completed", minutes_ago=60)

    board = await _build()

    assert [a.agent_id for a in board.agents] == ["fresh"]


async def test_a_run_still_running_from_last_week_is_not_active(mongo_db):  # noqa: ARG001
    """A leaked run must not pin an agent to ACTIVE forever.

    The window bounds the ACTIVE read too, so a run left ``running`` past the
    window (what ``find_stale_running`` reaps) drops off the board entirely
    rather than showing an agent that has not worked in days as working.
    """
    await _run(workspace="w1", agent_id="ghost", status="running", minutes_ago=60 * 24 * 7)

    board = await _build()

    assert board.agents == []


# ===========================================================================
# Ordering
# ===========================================================================


async def test_working_agents_sort_first_then_most_recent(mongo_db):  # noqa: ARG001
    await _run(workspace="w1", agent_id="idle-old", status="completed", minutes_ago=300)
    await _run(workspace="w1", agent_id="idle-new", status="completed", minutes_ago=10)
    await _run(workspace="w1", agent_id="busy", status="running", minutes_ago=200)

    board = await _build()

    assert [a.agent_id for a in board.agents] == ["busy", "idle-new", "idle-old"]


# ===========================================================================
# Cross-workspace isolation — the property this surface exists to hold
# ===========================================================================


async def test_service_never_returns_another_workspace(mongo_db):  # noqa: ARG001
    await _run(workspace="w1", agent_id="mine", status="running")
    await _run(workspace="w2", agent_id="theirs", status="running")
    await _run(workspace="w2", agent_id="theirs-too", status="failed", minutes_ago=1)

    w1 = await _build("w1")
    w2 = await _build("w2")

    assert [a.agent_id for a in w1.agents] == ["mine"]
    assert sorted(a.agent_id for a in w2.agents) == ["theirs", "theirs-too"]


async def test_same_agent_id_in_two_workspaces_does_not_bleed(mongo_db):  # noqa: ARG001
    """The nastier shape: the same agent id exists in both tenants.

    A missing workspace filter would merge the two into one entry — the run
    count and status would silently describe the other tenant's activity.
    """
    await _run(workspace="w1", agent_id="shared", status="completed", minutes_ago=30)
    await _run(workspace="w2", agent_id="shared", status="running", minutes_ago=5)
    await _run(workspace="w2", agent_id="shared", status="running", minutes_ago=6)

    w1 = await _build("w1")

    assert len(w1.agents) == 1
    entry = w1.agents[0]
    assert entry.status == AgentStatus.IDLE.value
    assert entry.active_runs == 0


# ===========================================================================
# Router / HTTP — real RBAC guard, real workspace dependency
# ===========================================================================


def _build_app(
    *,
    role: str | None = "member",
    active_workspace: str = "w1",
    memberships: list[str] | None = None,
) -> FastAPI:
    """App over the agent-activity router with the REAL RBAC guard.

    ``role=None`` leaves ``current_active_user`` un-overridden, so the real
    fastapi-users dependency runs and an anonymous request is rejected.
    """
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.agent_activity.router import router
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None

    if role is not None:
        user = SimpleNamespace(
            id="u1",
            active_workspace=active_workspace,
            workspaces=[
                SimpleNamespace(workspace=w, role=role) for w in (memberships or [active_workspace])
            ],
        )

        async def _fake_user():
            return user

        app.dependency_overrides[current_active_user] = _fake_user
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_endpoint_returns_the_board(mongo_db):  # noqa: ARG001
    await _run(workspace="w1", agent_id="a1", status="running", minutes_ago=2)

    async with _client(_build_app()) as client:
        resp = await client.get("/api/v1/agent-activity")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"agents", "ts"}
    assert body["agents"] == [
        {
            "agent_id": "a1",
            "status": "active",
            "active_runs": 1,
            "last_active": (NOW - timedelta(minutes=2)).isoformat(),
            "last_run_id": "r-w1-a1-running-2",
        }
    ]


async def test_endpoint_is_scoped_to_the_callers_workspace(mongo_db):  # noqa: ARG001
    """A caller who belongs to BOTH workspaces still sees only the active one.

    Membership is not the filter — the active workspace is. This is the test
    that would fail if the query ever stopped carrying the workspace.
    """
    await _run(workspace="w1", agent_id="mine", status="running")
    await _run(workspace="w2", agent_id="theirs", status="running")

    app = _build_app(active_workspace="w1", memberships=["w1", "w2"])
    async with _client(app) as client:
        resp = await client.get("/api/v1/agent-activity")

    assert resp.status_code == 200, resp.text
    assert [a["agent_id"] for a in resp.json()["agents"]] == ["mine"]


async def test_endpoint_empty_state(mongo_db):  # noqa: ARG001
    async with _client(_build_app()) as client:
        resp = await client.get("/api/v1/agent-activity")

    assert resp.status_code == 200, resp.text
    assert resp.json()["agents"] == []


async def test_workspace_id_query_param_is_rejected(mongo_db):  # noqa: ARG001
    """Tenancy comes from auth, never the query — asking is a 400, not a silent
    ignore, so nobody can later wire the param up by accident."""
    await _run(workspace="w2", agent_id="theirs", status="running")

    async with _client(_build_app()) as client:
        resp = await client.get("/api/v1/agent-activity", params={"workspace_id": "w2"})

    assert resp.status_code == 400, resp.text


async def test_member_is_allowed_and_anonymous_is_denied(mongo_db):  # noqa: ARG001
    """A plain MEMBER may read their own workspace's board (unlike the ADMIN-only
    herdr cockpit); an unauthenticated caller is rejected by the auth layer."""
    async with _client(_build_app(role="member")) as client:
        member = await client.get("/api/v1/agent-activity")
    assert member.status_code == 200, member.text

    async with _client(_build_app(role=None)) as client:
        anon = await client.get("/api/v1/agent-activity")
    assert anon.status_code == 401, anon.text


async def test_non_member_of_the_active_workspace_is_denied(mongo_db):  # noqa: ARG001
    """The RBAC guard resolves the role IN the active workspace. A user whose
    active workspace is one they hold no membership in gets 403, not a board."""
    app = _build_app(active_workspace="w9", memberships=["w1"])
    async with _client(app) as client:
        resp = await client.get("/api/v1/agent-activity")

    assert resp.status_code == 403, resp.text
