# Tests for task status persistence — reproduces the bug where
# changing status via the API doesn't persist after refresh.
# Created: 2026-02-12
#
# Root cause: update_task_status endpoint expects `status` as query param
# but frontend sends it as JSON body. FastAPI treats bare str params as
# query params, not body params.
#
# Also tests that projectTasks get updated alongside main tasks list.

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketclaw.deep_work.api import router as deep_work_router
from pocketclaw.mission_control import (
    FileMissionControlStore,
    MissionControlManager,
    reset_mission_control_manager,
    reset_mission_control_store,
)
from pocketclaw.mission_control.api import router as mc_router

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_store_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def test_app(temp_store_path, monkeypatch):
    reset_mission_control_store()
    reset_mission_control_manager()

    store = FileMissionControlStore(temp_store_path)
    manager = MissionControlManager(store)

    import pocketclaw.mission_control.manager as manager_module
    import pocketclaw.mission_control.store as store_module

    monkeypatch.setattr(store_module, "_store_instance", store)
    monkeypatch.setattr(manager_module, "_manager_instance", manager)

    app = FastAPI()
    app.include_router(mc_router, prefix="/api/mission-control")
    app.include_router(deep_work_router, prefix="/api/deep-work")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


@pytest.fixture
def manager(test_app):
    import pocketclaw.mission_control.manager as manager_module

    return manager_module._manager_instance


# ============================================================================
# Bug reproduction: status update via JSON body should persist
# ============================================================================


class TestTaskStatusPersistence:
    """Reproduce: POST /tasks/{id}/status with JSON body should persist."""

    def test_status_update_via_json_body(self, client):
        """Frontend sends {status: "done"} as JSON body — this must work."""
        # Create a task
        create_res = client.post(
            "/api/mission-control/tasks",
            json={"title": "Test persistence"},
        )
        assert create_res.status_code == 200
        task_id = create_res.json()["task"]["id"]
        assert create_res.json()["task"]["status"] == "inbox"

        # Update status the way the frontend does — JSON body
        status_res = client.post(
            f"/api/mission-control/tasks/{task_id}/status",
            json={"status": "done"},
        )
        assert status_res.status_code == 200
        assert status_res.json()["task"]["status"] == "done"

        # Verify it persisted — refetch the task (simulates page refresh)
        get_res = client.get(f"/api/mission-control/tasks/{task_id}")
        assert get_res.status_code == 200
        assert get_res.json()["task"]["status"] == "done"

    def test_status_update_sets_completed_at(self, client):
        """Setting status to 'done' should set completed_at timestamp."""
        create_res = client.post(
            "/api/mission-control/tasks",
            json={"title": "Completion timestamp"},
        )
        task_id = create_res.json()["task"]["id"]

        client.post(
            f"/api/mission-control/tasks/{task_id}/status",
            json={"status": "done"},
        )

        get_res = client.get(f"/api/mission-control/tasks/{task_id}")
        task = get_res.json()["task"]
        assert task["status"] == "done"
        assert task["completed_at"] is not None

    def test_status_update_to_skipped_via_json_body(self, client):
        """Setting status to 'skipped' via JSON body should persist."""
        create_res = client.post(
            "/api/mission-control/tasks",
            json={"title": "Skip me"},
        )
        task_id = create_res.json()["task"]["id"]

        status_res = client.post(
            f"/api/mission-control/tasks/{task_id}/status",
            json={"status": "skipped"},
        )
        assert status_res.status_code == 200
        assert status_res.json()["task"]["status"] == "skipped"

        # Verify persistence
        get_res = client.get(f"/api/mission-control/tasks/{task_id}")
        assert get_res.json()["task"]["status"] == "skipped"

    def test_status_update_round_trip(self, client):
        """Multiple status transitions should all persist."""
        create_res = client.post(
            "/api/mission-control/tasks",
            json={"title": "Round trip"},
        )
        task_id = create_res.json()["task"]["id"]

        for status in ["assigned", "in_progress", "review", "done"]:
            res = client.post(
                f"/api/mission-control/tasks/{task_id}/status",
                json={"status": status},
            )
            assert res.status_code == 200

            get_res = client.get(f"/api/mission-control/tasks/{task_id}")
            assert get_res.json()["task"]["status"] == status

    def test_status_update_invalid_status_returns_error(self, client):
        """Invalid status value should return 422 or 400."""
        create_res = client.post(
            "/api/mission-control/tasks",
            json={"title": "Invalid status"},
        )
        task_id = create_res.json()["task"]["id"]

        res = client.post(
            f"/api/mission-control/tasks/{task_id}/status",
            json={"status": "nonexistent"},
        )
        assert res.status_code in (400, 422, 500)

    def test_project_task_status_persists_after_refetch(self, client, manager):
        """Status change on a project task should survive plan refetch."""
        # Create a project and add a task
        proj_res = client.post(
            "/api/mission-control/projects",
            json={"title": "Persistence Project"},
        )
        project_id = proj_res.json()["project"]["id"]

        task_res = client.post(
            "/api/mission-control/tasks",
            json={"title": "Project task"},
        )
        task_id = task_res.json()["task"]["id"]

        # Link task to project by patching store directly
        import asyncio

        async def link():
            task = await manager.get_task(task_id)
            task.project_id = project_id
            await manager._store.save_task(task)

        asyncio.get_event_loop().run_until_complete(link())

        # Update status via JSON body
        client.post(
            f"/api/mission-control/tasks/{task_id}/status",
            json={"status": "done"},
        )

        # Fetch via plan endpoint (the way the project view does)
        plan_res = client.get(f"/api/deep-work/projects/{project_id}/plan")
        assert plan_res.status_code == 200
        tasks = plan_res.json()["tasks"]
        task_data = next((t for t in tasks if t["id"] == task_id), None)
        assert task_data is not None
        assert task_data["status"] == "done"
