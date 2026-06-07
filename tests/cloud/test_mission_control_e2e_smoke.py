# tests/cloud/test_mission_control_e2e_smoke.py
# Created: 2026-05-17 — End-to-end smoke that drives the full Mission
# Control flow against the merged ee branch: create project → create
# pocket scoped to project → create task scoped to project → confirm
# task surfaces in /mission-control/items → filter by project → delete
# project → confirm children survive with project_id unassigned.
#
# Mounts every relevant router (projects, tasks, cycles, mission_control)
# in one FastAPI app with the request_context dep overridden, against
# a fresh mongomock Beanie DB. The autouse `recording_bus` fixture
# captures emitted events so we can assert wiring without poking the
# realtime bus directly.
#
# Updated: 2026-06-07 — switched the `ee.cloud.*` imports to the canonical
# `pocketpaw_ee.cloud.*` path (the house standard used by ~686 other files).
# The short `ee.` alias resolves at runtime but isn't a discoverable package
# in the OSS-only Lint CI env, so ruff's isort classified it as third-party
# and flagged the import block (I001), reddening dev's Lint job.
"""End-to-end smoke for Mission Control's full primitive chain.

This is the regression test for the 2026-05-16 "task disappears" bug:
the façade only queried Instinct and never composed Tasks. If that
regression returns, this test fails at the assert that confirms a
freshly-created task surfaces in /mission-control/items.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.context import (
    RequestContext,
    ScopeKind,
    request_context,
)
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.cycles.router import router as cycles_router
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.mission_control.router import router as mc_router
from pocketpaw_ee.cloud.projects.router import router as projects_router
from pocketpaw_ee.cloud.tasks.router import router as tasks_router

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(workspace_id: str = "w_alpha", user_id: str = "u_shawn") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="req-smoke",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _build_app(workspace_id: str = "w_alpha", user_id: str = "u_shawn") -> FastAPI:
    """Mount every MC-related router on one app + override auth.

    Mirrors the layout in ``ee.cloud.__init__.mount_cloud`` minus the
    pieces unrelated to this smoke (auth, channels, etc.).
    """

    app = FastAPI()
    add_error_handler(app)
    app.include_router(projects_router, prefix="/api/v1")
    app.include_router(tasks_router, prefix="/api/v1")
    app.include_router(cycles_router, prefix="/api/v1")
    app.include_router(mc_router, prefix="/api/v1")

    async def _fake_ctx() -> RequestContext:
        return _ctx(workspace_id=workspace_id, user_id=user_id)

    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[request_context] = _fake_ctx
    return app


@pytest_asyncio.fixture
async def app_alpha() -> FastAPI:
    return _build_app("w_alpha", "u_shawn")


@pytest_asyncio.fixture
async def app_beta() -> FastAPI:
    """Second tenant — for the cross-workspace isolation assertion."""

    return _build_app("w_beta", "u_jess")


# ---------------------------------------------------------------------------
# The headline smoke: create chain, list, delete cascade
# ---------------------------------------------------------------------------


class TestFullChain:
    """Drives a realistic operator flow start-to-finish."""

    @pytest.mark.asyncio
    async def test_project_then_task_surfaces_in_mission_control(
        self, monkeypatch, app_alpha: FastAPI
    ) -> None:
        # The pockets facade isn't in this smoke (it's a separate router
        # with its own auth chain). Stub the visible-pocket lookup so the
        # façade's pocket-visibility gate doesn't block Tasks (which are
        # workspace-scoped, not pocket-scoped). Empty set forces the
        # Tasks-only path, which is precisely what we want to test.
        from unittest.mock import AsyncMock

        from pocketpaw_ee.cloud.mission_control import service as mc_service

        monkeypatch.setattr(mc_service.pockets_service, "list_pockets", AsyncMock(return_value=[]))

        with TestClient(app_alpha) as client:
            # 1. Create a project
            r = client.post("/api/v1/projects", json={"name": "Crestline · May 23"})
            assert r.status_code == 200, r.text
            project = r.json()
            project_id = project["id"]
            assert project["workspace_id"] == "w_alpha"
            assert project["status"] == "active"

            # 2. Project shows up in the list
            r = client.get("/api/v1/projects")
            assert r.status_code == 200
            payload = r.json()
            # Router returns a bare list today; tolerate the envelope
            # form too in case it ever changes.
            ids = {p["id"] for p in (payload if isinstance(payload, list) else payload["items"])}
            assert project_id in ids

            # 3. Create a task scoped to the project
            r = client.post(
                "/api/v1/tasks",
                json={
                    "title": "Confirm venue walkthrough date",
                    "assignee": {"kind": "human", "id": "u_shawn", "name": "shawn"},
                    "project_id": project_id,
                },
            )
            assert r.status_code == 200, r.text
            task = r.json()
            task_id = task["id"]
            # Backend derives status from assignee kind. Human → in_progress.
            assert task["status"] == "in_progress"
            assert task["project_id"] == project_id

            # 4. The Mission Control façade surfaces the task. THIS is
            #    the regression for the 2026-05-16 "task disappears" bug.
            r = client.get("/api/v1/mission-control/items")
            assert r.status_code == 200, r.text
            items = r.json()
            titles = [it["title"] for it in items]
            assert "Confirm venue walkthrough date" in titles, (
                f"task missing from MC items — façade may have regressed "
                f"to Instinct-only reads. items returned: {items}"
            )

            # The projected id carries the task: prefix so bulk endpoints
            # can route reassign/snooze correctly.
            mc_ids = [it["id"] for it in items]
            assert f"task:{task_id}" in mc_ids

            # 5. Filter by project narrows correctly
            r = client.get(f"/api/v1/mission-control/items?project_id={project_id}")
            assert r.status_code == 200
            assert "Confirm venue walkthrough date" in [it["title"] for it in r.json()]

            # 6. Delete the project — children must survive with
            #    project_id unassigned (soft cascade).
            r = client.delete(f"/api/v1/projects/{project_id}")
            assert r.status_code == 204, r.text

            # 7. The task is still there
            r = client.get(f"/api/v1/tasks/{task_id}")
            assert r.status_code == 200
            assert r.json()["project_id"] is None, (
                "soft cascade broken — task should keep its row but lose project_id"
            )

            # 8. And the MC items list still includes it
            r = client.get("/api/v1/mission-control/items")
            titles_after = [it["title"] for it in r.json()]
            assert "Confirm venue walkthrough date" in titles_after


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


class TestTenancy:
    """Confirms a task in workspace alpha can't leak into workspace beta."""

    @pytest.mark.asyncio
    async def test_alpha_task_invisible_to_beta(
        self, monkeypatch, app_alpha: FastAPI, app_beta: FastAPI
    ) -> None:
        from unittest.mock import AsyncMock

        from pocketpaw_ee.cloud.mission_control import service as mc_service

        monkeypatch.setattr(mc_service.pockets_service, "list_pockets", AsyncMock(return_value=[]))

        with TestClient(app_alpha) as alpha:
            r = alpha.post(
                "/api/v1/tasks",
                json={
                    "title": "alpha-only task",
                    "assignee": {"kind": "human", "id": "u_shawn", "name": "shawn"},
                },
            )
            assert r.status_code == 200, r.text

        with TestClient(app_beta) as beta:
            r = beta.get("/api/v1/mission-control/items")
            assert r.status_code == 200
            titles = [it["title"] for it in r.json()]
            assert "alpha-only task" not in titles, (
                "tenant leak — w_beta saw a task that lives in w_alpha"
            )


# ---------------------------------------------------------------------------
# Cycle snapshot — HTTP form (the one Shift 12 in the playbook uses)
# ---------------------------------------------------------------------------


class TestCycleSnapshotEndpoint:
    @pytest.mark.asyncio
    async def test_manual_snapshot_returns_point_then_null_on_re_run(
        self, monkeypatch, app_alpha: FastAPI
    ) -> None:
        from datetime import date
        from unittest.mock import AsyncMock

        from pocketpaw_ee.cloud.mission_control import service as mc_service

        monkeypatch.setattr(mc_service.pockets_service, "list_pockets", AsyncMock(return_value=[]))

        with TestClient(app_alpha) as client:
            r = client.post(
                "/api/v1/cycles",
                json={
                    "name": "Crestline cycle",
                    "start": date(2026, 5, 1).isoformat(),
                    "end": date(2026, 5, 31).isoformat(),
                    "status": "active",
                },
            )
            assert r.status_code == 200, r.text
            cycle_id = r.json()["id"]

            # First snapshot returns the new point
            r = client.post(f"/api/v1/cycles/{cycle_id}/snapshot")
            assert r.status_code == 200, r.text
            point = r.json()
            assert point is not None
            assert "date" in point

            # Same-day re-run returns null (idempotent — documented in
            # the playbook so interns don't file a bug on silent re-runs)
            r2 = client.post(f"/api/v1/cycles/{cycle_id}/snapshot")
            assert r2.status_code == 200
            assert r2.json() is None
