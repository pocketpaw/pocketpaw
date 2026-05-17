# Created: 2026-05-17 — pocketpaw#1118 P1. HTTP-layer tests for the
#   planner router. Smokes the endpoint surface end-to-end through a
#   FastAPI app: status mapping, tenant override, 204 on missing plan.
#   Service-level coverage lives in test_planner_service.py.
"""HTTP-layer tests for ``ee/cloud/planner/router.py``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud._core.context import RequestContext, ScopeKind, request_context
from ee.cloud._core.http import add_error_handler
from ee.cloud.license import require_license
from ee.cloud.planner.router import router as planner_router
from ee.cloud.projects import service as projects_service
from ee.cloud.projects.dto import CreateProjectRequest


def _make_ctx(workspace_id: str | None, user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _build_app(workspace_id: str | None = "w1", user_id: str = "u1") -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(planner_router)

    async def _ctx() -> RequestContext:
        return _make_ctx(workspace_id, user_id)

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[require_license] = lambda: None
    return app


class _FakePlannerResult:
    """Minimal PlannerResult duck-type — see test_planner_service for
    rationale on not importing the OSS dataclass.
    """

    def __init__(self) -> None:
        self.prd_content = "# PRD"
        self.tasks: list = []
        self.human_tasks: list = []
        self.team_recommendation: list = []
        self.dependency_graph: dict = {}
        self.research_notes = ""
        self.project_id = ""
        self.estimated_total_minutes = 0

    def to_dict(self) -> dict:
        return {"prd_content": self.prd_content, "tasks": []}


def _patched_planner():
    fake = _FakePlannerResult()

    async def _fake_plan(self, project_description, project_id="", research_depth="standard"):  # noqa: ARG001
        fake.project_id = project_id
        return fake

    return patch(
        "pocketpaw.deep_work.planner.PlannerAgent.plan",
        new=_fake_plan,
    )


@pytest.fixture(autouse=True)
def _isolated_upload_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest_asyncio.fixture
async def mongo_only(mongo_db: Any):
    yield mongo_db


@pytest_asyncio.fixture
async def w1_client(mongo_only) -> AsyncClient:
    app = _build_app(workspace_id="w1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2_client(mongo_only) -> AsyncClient:
    app = _build_app(workspace_id="w2")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _create_project(workspace_id: str = "w1") -> str:
    proj = await projects_service.agent_create(
        _make_ctx(workspace_id),
        CreateProjectRequest(name="P"),
    )
    return proj.id


# ---------------------------------------------------------------------------
# POST /planner/run
# ---------------------------------------------------------------------------


async def test_run_planner_returns_result(w1_client: AsyncClient) -> None:
    project_id = await _create_project()
    with _patched_planner():
        resp = await w1_client.post(
            "/planner/run",
            json={
                "project_id": project_id,
                "goal": "Plan a wedding for May with three vendors.",
                "deep_research": False,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project_id"] == project_id
    assert body["status"] == "ready"
    assert body["prd_file_id"]


async def test_run_planner_validates_short_goal(w1_client: AsyncClient) -> None:
    """Goals shorter than the min length get rejected at the DTO layer."""

    project_id = await _create_project()
    resp = await w1_client.post(
        "/planner/run",
        json={"project_id": project_id, "goal": "hi", "deep_research": False},
    )
    assert resp.status_code == 422  # FastAPI Pydantic validation failure


async def test_run_planner_blocks_other_workspace_project(
    w1_client: AsyncClient, w2_client: AsyncClient
) -> None:
    project_id = await _create_project(workspace_id="w1")
    with _patched_planner():
        resp = await w2_client.post(
            "/planner/run",
            json={
                "project_id": project_id,
                "goal": "Plan something with enough characters.",
            },
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /planner/by-project/{id}
# ---------------------------------------------------------------------------


async def test_get_plan_returns_204_when_missing(w1_client: AsyncClient) -> None:
    project_id = await _create_project()
    resp = await w1_client.get(f"/planner/by-project/{project_id}")
    assert resp.status_code == 204


async def test_get_plan_returns_summary_after_run(w1_client: AsyncClient) -> None:
    project_id = await _create_project()
    with _patched_planner():
        run_resp = await w1_client.post(
            "/planner/run",
            json={
                "project_id": project_id,
                "goal": "Plan a wedding for May with three vendors.",
            },
        )
    assert run_resp.status_code == 200
    summary_resp = await w1_client.get(f"/planner/by-project/{project_id}")
    assert summary_resp.status_code == 200
    body = summary_resp.json()
    assert body["project_id"] == project_id
    assert body["prd_file_id"] == run_resp.json()["prd_file_id"]
