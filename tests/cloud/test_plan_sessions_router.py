# test_plan_sessions_router.py — HTTP-layer tests for
#   ee/cloud/mission_control/router.py::list_plan_sessions.
# Created: 2026-05-18 (feat/mc-plan-sessions-endpoint) — Smokes the new
#   /api/v1/mission-control/plan-sessions surface: tenancy isolation,
#   query-param leak guard (?workspace_id=), status + limit filters,
#   response envelope parity with the spec, and the auth seam. Service-
#   level mapping (ready → draft, stale → archived) is covered here at
#   the wire boundary since the router is thin.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud._core.context import RequestContext, ScopeKind, request_context
from ee.cloud._core.http import add_error_handler
from ee.cloud.license import require_license
from ee.cloud.mission_control.router import router as mc_router

pytestmark = pytest.mark.usefixtures("mongo_db")


def _build_app(workspace_id: str | None = "w1", user_id: str = "u1") -> FastAPI:
    """Mount the Mission Control router with the request_context dep
    overridden so we don't need a real JWT chain.

    Mirrors the planner / mission_control router test scaffolding so the
    test surface is consistent across the three sibling Plan-tab tests.
    """
    app = FastAPI()
    add_error_handler(app)
    app.include_router(mc_router, prefix="/api/v1")

    async def _fake_ctx() -> RequestContext:
        return RequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            request_id="req-test",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _fake_ctx
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest_asyncio.fixture
async def w1_client() -> AsyncClient:
    transport = ASGITransport(app=_build_app(workspace_id="w1"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2_client() -> AsyncClient:
    transport = ASGITransport(app=_build_app(workspace_id="w2"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


# ---------------------------------------------------------------------------
# Empty + tenancy
# ---------------------------------------------------------------------------


async def test_returns_empty_for_fresh_workspace(w1_client: AsyncClient) -> None:
    """A workspace with no plan sessions returns the canonical empty
    envelope — never 500, never null."""
    r = await w1_client.get("/api/v1/mission-control/plan-sessions")
    assert r.status_code == 200, r.text
    assert r.json() == {"sessions": [], "total": 0}


async def test_returns_workspace_sessions_only(w1_client: AsyncClient, make_plan_session) -> None:
    """The listing surfaces only sessions owned by the active workspace."""
    await make_plan_session("w1", name="Q2 Marketing Plan", task_ids=["t1", "t2"])
    await make_plan_session("w2", name="Other Tenant Plan", task_ids=["x1"])
    r = await w1_client.get("/api/v1/mission-control/plan-sessions")
    assert r.status_code == 200
    body = r.json()
    names = {s["name"] for s in body["sessions"]}
    assert names == {"Q2 Marketing Plan"}
    assert body["total"] == 1


async def test_w2_cannot_see_w1_sessions(w2_client: AsyncClient, make_plan_session) -> None:
    """Cross-tenant probe — w2 sees nothing of w1's drafts."""
    await make_plan_session("w1", name="w1-only")
    r = await w2_client.get("/api/v1/mission-control/plan-sessions")
    assert r.status_code == 200
    assert r.json() == {"sessions": [], "total": 0}


# ---------------------------------------------------------------------------
# Query-param leak guard
# ---------------------------------------------------------------------------


async def test_rejects_workspace_id_query_param(w1_client: AsyncClient, make_plan_session) -> None:
    """Passing ``?workspace_id=`` is a 400 — workspace lives on the auth
    ctx, never on the query string."""
    await make_plan_session("w2", name="forbidden")
    r = await w1_client.get(
        "/api/v1/mission-control/plan-sessions",
        params={"workspace_id": "w2"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "plan_sessions.workspace_id_forbidden"


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


async def test_status_filter_forwards(w1_client: AsyncClient, make_plan_session) -> None:
    """``?status=draft`` filters down to ``ready`` plan sessions; the
    service maps the wire vocabulary to the doc-level statuses."""
    await make_plan_session("w1", name="Current Plan", status="ready")
    await make_plan_session("w1", name="Old Plan", status="stale")
    r = await w1_client.get("/api/v1/mission-control/plan-sessions", params={"status": "draft"})
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["name"] == "Current Plan"
    assert sessions[0]["status"] == "draft"


async def test_status_archived_filters_to_stale(w1_client: AsyncClient, make_plan_session) -> None:
    """``?status=archived`` only surfaces stale (superseded) plans."""
    await make_plan_session("w1", name="Current", status="ready")
    await make_plan_session("w1", name="Superseded", status="stale")
    r = await w1_client.get("/api/v1/mission-control/plan-sessions", params={"status": "archived"})
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["name"] == "Superseded"
    assert sessions[0]["status"] == "archived"


async def test_limit_forwards(w1_client: AsyncClient, make_plan_session) -> None:
    """``?limit=N`` caps the returned list."""
    for i in range(5):
        await make_plan_session("w1", name=f"Plan {i}")
    r = await w1_client.get("/api/v1/mission-control/plan-sessions", params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert len(body["sessions"]) == 2
    assert body["total"] == 2


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


async def test_envelope_field_parity(w1_client: AsyncClient, make_plan_session) -> None:
    """The response carries exactly the keys the spec promises — never
    more, never less. Frontend mappers depend on this verbatim."""
    await make_plan_session("w1", name="Spec Plan", task_ids=["a", "b", "c"])
    r = await w1_client.get("/api/v1/mission-control/plan-sessions")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"sessions", "total"}
    assert len(body["sessions"]) == 1
    session = body["sessions"][0]
    expected_keys = {"id", "name", "status", "task_count", "created_at", "updated_at"}
    assert set(session.keys()) == expected_keys
    assert session["name"] == "Spec Plan"
    assert session["task_count"] == 3
    assert session["status"] == "draft"  # ready → draft mapping


# ---------------------------------------------------------------------------
# Auth / context invariants
# ---------------------------------------------------------------------------


async def test_missing_auth_returns_401() -> None:
    """Without a ``request_context`` override the fastapi-users auth
    chain runs against a request with no Bearer/cookie — short-circuits
    to 401 before the handler runs."""
    app = FastAPI()
    add_error_handler(app)
    app.include_router(mc_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/v1/mission-control/plan-sessions")
        assert r.status_code == 401


async def test_ctx_without_workspace_returns_empty(make_plan_session) -> None:
    """A request whose ctx has no active workspace must NOT 500 and must
    NOT leak rows from any other tenant — it returns the empty envelope.
    Mirrors the audit service invariant."""
    await make_plan_session("w1", name="leaky")
    transport = ASGITransport(app=_build_app(workspace_id=None))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/v1/mission-control/plan-sessions")
        assert r.status_code == 200, r.text
        assert r.json() == {"sessions": [], "total": 0}


# Silence ruff unused-import nudge — these are imported for fixture
# composition (the make_plan_session fixture is request-injected).
_unused: tuple[Any, ...] = ()
