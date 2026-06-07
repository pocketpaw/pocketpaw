# test_mission_control_create_cycle.py — HTTP-layer tests for
#   ee/cloud/mission_control/router.py::create_cycle (POST /cycles).
# Created: 2026-05-19 (feat/mc-create-cycle-endpoint) — smokes the new
#   /api/v1/mission-control/cycles façade endpoint: tenancy isolation,
#   query-param leak guard, status derivation from dates, project
#   tenancy, event emission, and DTO validation edges. The actual
#   Beanie write happens in ``cycles.service.agent_create_cycle`` —
#   tested separately in test_cycles_service.py; this file only covers
#   the façade wiring + status-derivation contract.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.mission_control.router import router as mc_router

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# App + client builders — mirror the plan-sessions test scaffolding so all
# three Mission Control HTTP suites stay readable side-by-side.
# ---------------------------------------------------------------------------


def _build_app(workspace_id: str | None = "w1", user_id: str = "u1") -> FastAPI:
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


def _today_str() -> str:
    return datetime.now(UTC).date().isoformat()


def _future_str(days: int) -> str:
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


def _past_str(days: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()


def make_cycle_request(**overrides: Any) -> dict[str, Any]:
    """Factory for a valid POST /mission-control/cycles body.

    Defaults to a future-dated cycle ("+New cycle" form's most common
    flow). Tests override only the fields they care about.
    """
    body: dict[str, Any] = {
        "name": "Spring Engagement",
        "start": _future_str(1),
        "end": _future_str(15),
        "project_id": None,
        "scope": 0,
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1. Happy path — response shape + persistence
# ---------------------------------------------------------------------------


async def test_create_cycle_returns_response_shape(w1_client: AsyncClient) -> None:
    """Happy path — verify all CycleResponse fields are populated and the
    frontend can ``cycles.unshift(response)`` without re-fetch."""
    body = make_cycle_request(name="May Wedding Prep", scope=12)
    r = await w1_client.post("/api/v1/mission-control/cycles", json=body)
    assert r.status_code == 200, r.text
    data = r.json()

    # Mirrors CycleResponse → CycleListItemResponse field set.
    expected_keys = {
        "id",
        "workspace_id",
        "name",
        "description",
        "pocket_id",
        "project_id",
        "start",
        "end",
        "status",
        "scope",
        "started",
        "completed",
        "created_by",
        "created_at",
        "updated_at",
        "daily",
    }
    assert set(data.keys()) == expected_keys
    assert data["name"] == "May Wedding Prep"
    assert data["workspace_id"] == "w1"
    assert data["scope"] == 12  # operator-supplied target seeded
    assert data["started"] == 0
    assert data["completed"] == 0
    assert data["daily"] == []
    assert data["created_by"] == "u1"
    assert data["id"]  # opaque ObjectId string


# ---------------------------------------------------------------------------
# 2. Cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_create_persists_for_workspace_only(
    w1_client: AsyncClient, w2_client: AsyncClient
) -> None:
    """w1 creates a cycle; w2 sees nothing in its workspace listing."""
    r = await w1_client.post(
        "/api/v1/mission-control/cycles",
        json=make_cycle_request(name="W1 cycle"),
    )
    assert r.status_code == 200
    cycle_id = r.json()["id"]

    # Pull w1's cycles via the cycles read endpoint (it's the simplest
    # cross-tenant probe; the MC façade has its own list flow but the
    # service is shared so this is sufficient for tenancy proof).
    from pocketpaw_ee.cloud.cycles.router import router as cycles_router

    w1_app = _build_app(workspace_id="w1")
    w1_app.include_router(cycles_router)
    w2_app = _build_app(workspace_id="w2")
    w2_app.include_router(cycles_router)

    async with AsyncClient(transport=ASGITransport(app=w1_app), base_url="http://t") as w1c:
        listing = await w1c.get("/cycles")
        assert listing.status_code == 200
        assert any(c["id"] == cycle_id for c in listing.json())

    async with AsyncClient(transport=ASGITransport(app=w2_app), base_url="http://t") as w2c:
        listing = await w2c.get("/cycles")
        assert listing.status_code == 200
        assert all(c["id"] != cycle_id for c in listing.json())


# ---------------------------------------------------------------------------
# 3. ?workspace_id query-param leak guard
# ---------------------------------------------------------------------------


async def test_rejects_workspace_id_query_param(w1_client: AsyncClient) -> None:
    """Passing ``?workspace_id=w2`` is a 400 — tenancy must come from the
    auth context, never the query string. Mirrors the audit + plan-sessions
    pattern."""
    r = await w1_client.post(
        "/api/v1/mission-control/cycles",
        params={"workspace_id": "w2"},
        json=make_cycle_request(),
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "cycles.workspace_id_forbidden"


# ---------------------------------------------------------------------------
# 4. Invalid date range
# ---------------------------------------------------------------------------


async def test_invalid_date_range_rejected(w1_client: AsyncClient) -> None:
    """``start >= end`` is a 422 with the documented machine-readable code."""
    body = make_cycle_request(start=_future_str(10), end=_future_str(5))
    r = await w1_client.post("/api/v1/mission-control/cycles", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "cycle.invalid_date_range"


# ---------------------------------------------------------------------------
# 5 + 6. Status derivation
# ---------------------------------------------------------------------------


async def test_status_derives_from_dates_upcoming(w1_client: AsyncClient) -> None:
    """A cycle whose start is in the future surfaces as ``upcoming``."""
    body = make_cycle_request(start=_future_str(3), end=_future_str(20))
    r = await w1_client.post("/api/v1/mission-control/cycles", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "upcoming"


async def test_status_derives_from_dates_active(w1_client: AsyncClient) -> None:
    """A cycle whose start has passed and end is still in the future
    surfaces as ``active`` — the daily snapshot job picks it up
    immediately."""
    body = make_cycle_request(start=_past_str(2), end=_future_str(10))
    r = await w1_client.post("/api/v1/mission-control/cycles", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"


# ---------------------------------------------------------------------------
# 7. Project tenancy
# ---------------------------------------------------------------------------


async def test_project_tenancy_enforced(w1_client: AsyncClient, w2_client: AsyncClient) -> None:
    """A project belonging to w2 cannot be referenced from w1's create
    call. The cycles service surfaces this as 404 ``project.not_found``
    rather than 403 — by design, so cross-tenant probes can't infer the
    existence of an id they don't own."""
    # Seed a project in w2 directly via the Beanie doc (the projects
    # service write path is tested elsewhere; here we only need a
    # populated project_id that w1 can't claim).
    from pocketpaw_ee.cloud.models.project import Project as _ProjectDoc

    proj = _ProjectDoc(
        workspace="w2",
        name="W2 Project",
        description="",
        color="",
        lead_id=None,
        status="active",
        created_by="u-w2",
    )
    await proj.insert()
    foreign_project_id = str(proj.id)

    body = make_cycle_request(project_id=foreign_project_id)
    r = await w1_client.post("/api/v1/mission-control/cycles", json=body)
    # The cycles service uses NotFound for cross-tenant project refs
    # — we mirror that contract so the rail's error renderer matches
    # whatever the existing cycles POST surfaces.
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "project.not_found"


# ---------------------------------------------------------------------------
# 8. Bus event emission
# ---------------------------------------------------------------------------


async def test_emits_cycle_created_event(w1_client: AsyncClient, recording_bus: Any) -> None:
    """The cycles service emits ``cycle.created`` on the bus; the façade
    delegates through so the same event fires for the rail flow. The
    frontend's live activity ticker depends on this for the
    just-created row to appear instantly."""
    r = await w1_client.post(
        "/api/v1/mission-control/cycles",
        json=make_cycle_request(name="Bus Event Cycle"),
    )
    assert r.status_code == 200
    response_id = r.json()["id"]

    created_events = [e for e in recording_bus.events if e.EVENT_TYPE == "cycle.created"]
    assert len(created_events) == 1
    payload = created_events[0].data
    assert payload["id"] == response_id
    assert payload["workspace_id"] == "w1"
    assert payload["name"] == "Bus Event Cycle"


# ---------------------------------------------------------------------------
# 9. Auth seam
# ---------------------------------------------------------------------------


async def test_missing_auth_returns_401() -> None:
    """Without a ``request_context`` override the fastapi-users auth
    chain runs against a request with no Bearer/cookie — short-circuits
    to 401 before the handler runs. Same invariant as the plan-sessions
    auth test."""
    app = FastAPI()
    add_error_handler(app)
    app.include_router(mc_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post("/api/v1/mission-control/cycles", json=make_cycle_request())
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# 10. Validation edges
# ---------------------------------------------------------------------------


async def test_name_too_long_rejected(w1_client: AsyncClient) -> None:
    """The 200-char name cap from the spec is enforced — Pydantic returns
    422 with the FastAPI validation envelope."""
    body = make_cycle_request(name="x" * 201)
    r = await w1_client.post("/api/v1/mission-control/cycles", json=body)
    assert r.status_code == 422, r.text


async def test_invalid_date_string_rejected(w1_client: AsyncClient) -> None:
    """A malformed ISO string surfaces as a clean 422 ``cycle.invalid_date``
    rather than a 500. The frontend renders the message directly."""
    body = make_cycle_request(start="not-a-date", end=_future_str(10))
    r = await w1_client.post("/api/v1/mission-control/cycles", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "cycle.invalid_date"


async def test_accepts_datetime_strings(w1_client: AsyncClient) -> None:
    """ISO datetime strings (the common JS toISOString() output) are
    accepted alongside bare dates — both produce the same stored cycle
    so the frontend can post either form without coercion."""
    start_dt = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    end_dt = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    body = make_cycle_request(start=start_dt, end=end_dt)
    r = await w1_client.post("/api/v1/mission-control/cycles", json=body)
    assert r.status_code == 200, r.text
    # The wire response stores dates as ISO date strings, not datetimes.
    assert "T" not in r.json()["start"]
    assert "T" not in r.json()["end"]


# Silence ruff unused-import nudges — fixtures are request-injected.
_unused: tuple[Any, ...] = ()
