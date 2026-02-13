# Tests for Deep Work project CRUD — Manager + API endpoints
# Created: 2026-02-12
# Tests project lifecycle via MissionControlManager and FastAPI endpoints

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketclaw.mission_control import (
    FileMissionControlStore,
    MissionControlManager,
    reset_mission_control_manager,
    reset_mission_control_store,
)
from pocketclaw.mission_control.api import router
from pocketclaw.mission_control.models import TaskStatus

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_store_path():
    """Create a temporary directory for test storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def test_app(temp_store_path, monkeypatch):
    """Create a test FastAPI app with Mission Control router."""
    reset_mission_control_store()
    reset_mission_control_manager()

    store = FileMissionControlStore(temp_store_path)
    manager = MissionControlManager(store)

    import pocketclaw.mission_control.manager as manager_module
    import pocketclaw.mission_control.store as store_module

    monkeypatch.setattr(store_module, "_store_instance", store)
    monkeypatch.setattr(manager_module, "_manager_instance", manager)

    app = FastAPI()
    app.include_router(router, prefix="/api/mission-control")

    return app


@pytest.fixture
def client(test_app):
    """Create a test client."""
    return TestClient(test_app)


@pytest.fixture
def manager(test_app, monkeypatch):
    """Get the test manager instance."""
    import pocketclaw.mission_control.manager as manager_module

    return manager_module._manager_instance


# ============================================================================
# Manager Tests
# ============================================================================


class TestProjectManager:
    """Tests for project operations on MissionControlManager."""

    @pytest.mark.asyncio
    async def test_create_project(self, manager):
        """Test creating a project via the manager."""
        project = await manager.create_project(
            title="Test Project",
            description="A test project",
            tags=["test", "demo"],
        )

        assert project.title == "Test Project"
        assert project.description == "A test project"
        assert project.tags == ["test", "demo"]
        assert project.creator_id == "human"
        assert project.status.value == "draft"
        assert project.id is not None

    @pytest.mark.asyncio
    async def test_get_project(self, manager):
        """Test retrieving a project by ID."""
        created = await manager.create_project(title="Fetch Me")
        fetched = await manager.get_project(created.id)

        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.title == "Fetch Me"

    @pytest.mark.asyncio
    async def test_get_project_not_found(self, manager):
        """Test retrieving a non-existent project."""
        result = await manager.get_project("nonexistent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_projects(self, manager):
        """Test listing all projects."""
        await manager.create_project(title="Project A")
        await manager.create_project(title="Project B")

        projects = await manager.list_projects()
        assert len(projects) == 2

    @pytest.mark.asyncio
    async def test_list_projects_by_status(self, manager):
        """Test listing projects filtered by status."""
        p1 = await manager.create_project(title="Draft Project")
        p2 = await manager.create_project(title="Approved Project")

        # Move p2 to approved
        from pocketclaw.deep_work.models import ProjectStatus

        p2.status = ProjectStatus.APPROVED
        await manager.update_project(p2)

        drafts = await manager.list_projects(status="draft")
        assert len(drafts) == 1
        assert drafts[0].id == p1.id

        approved = await manager.list_projects(status="approved")
        assert len(approved) == 1
        assert approved[0].id == p2.id

    @pytest.mark.asyncio
    async def test_get_project_tasks(self, manager):
        """Test getting tasks that belong to a project."""
        project = await manager.create_project(title="Task Parent")

        # Create tasks — some with project_id, some without
        t1 = await manager.create_task(title="Task 1")
        t1.project_id = project.id
        await manager._store.save_task(t1)

        t2 = await manager.create_task(title="Task 2")
        t2.project_id = project.id
        await manager._store.save_task(t2)

        await manager.create_task(title="Unrelated Task")

        tasks = await manager.get_project_tasks(project.id)
        assert len(tasks) == 2
        task_titles = {t.title for t in tasks}
        assert "Task 1" in task_titles
        assert "Task 2" in task_titles

    @pytest.mark.asyncio
    async def test_get_project_progress(self, manager):
        """Test progress returns correct counts with mixed statuses/types."""
        project = await manager.create_project(title="Progress Test")

        # Create tasks with different statuses and types
        # 1: agent task, done
        t1 = await manager.create_task(title="Agent Done")
        t1.project_id = project.id
        t1.task_type = "agent"
        t1.status = TaskStatus.DONE
        await manager._store.save_task(t1)

        # 2: agent task, in_progress
        t2 = await manager.create_task(title="Agent WIP")
        t2.project_id = project.id
        t2.task_type = "agent"
        t2.status = TaskStatus.IN_PROGRESS
        await manager._store.save_task(t2)

        # 3: human task, inbox (pending)
        t3 = await manager.create_task(title="Human Pending")
        t3.project_id = project.id
        t3.task_type = "human"
        t3.status = TaskStatus.INBOX
        await manager._store.save_task(t3)

        # 4: human task, done
        t4 = await manager.create_task(title="Human Done")
        t4.project_id = project.id
        t4.task_type = "human"
        t4.status = TaskStatus.DONE
        await manager._store.save_task(t4)

        # 5: agent task, blocked
        t5 = await manager.create_task(title="Agent Blocked")
        t5.project_id = project.id
        t5.task_type = "agent"
        t5.status = TaskStatus.BLOCKED
        await manager._store.save_task(t5)

        progress = await manager.get_project_progress(project.id)

        assert progress["total"] == 5
        assert progress["completed"] == 2
        assert progress["in_progress"] == 1
        assert progress["blocked"] == 1
        assert progress["human_pending"] == 1  # only t3 (human + not done)
        assert progress["percent"] == 40.0  # 2/5 * 100

    @pytest.mark.asyncio
    async def test_get_project_progress_empty(self, manager):
        """Test progress for a project with no tasks."""
        project = await manager.create_project(title="Empty Project")
        progress = await manager.get_project_progress(project.id)

        assert progress["total"] == 0
        assert progress["completed"] == 0
        assert progress["percent"] == 0.0

    @pytest.mark.asyncio
    async def test_delete_project(self, manager):
        """Test deleting a project also deletes its tasks."""
        project = await manager.create_project(title="Delete Me")

        # Add tasks to the project
        t1 = await manager.create_task(title="Project Task 1")
        t1.project_id = project.id
        await manager._store.save_task(t1)

        t2 = await manager.create_task(title="Project Task 2")
        t2.project_id = project.id
        await manager._store.save_task(t2)

        # Also create a task NOT in this project
        unrelated = await manager.create_task(title="Unrelated Task")

        deleted = await manager.delete_project(project.id)
        assert deleted is True

        # Verify project gone
        fetched = await manager.get_project(project.id)
        assert fetched is None

        # Verify project tasks deleted
        assert await manager.get_task(t1.id) is None
        assert await manager.get_task(t2.id) is None

        # Verify unrelated task still exists
        assert await manager.get_task(unrelated.id) is not None

    @pytest.mark.asyncio
    async def test_delete_project_not_found(self, manager):
        """Test deleting a non-existent project."""
        deleted = await manager.delete_project("nonexistent-id")
        assert deleted is False


# ============================================================================
# API Tests
# ============================================================================


class TestProjectAPI:
    """Tests for project REST endpoints."""

    def test_create_project(self, client):
        """Test creating a project via API."""
        response = client.post(
            "/api/mission-control/projects",
            json={
                "title": "API Project",
                "description": "Created via API",
                "tags": ["api", "test"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["project"]["title"] == "API Project"
        assert data["project"]["description"] == "Created via API"
        assert data["project"]["tags"] == ["api", "test"]
        assert data["project"]["status"] == "draft"

    def test_create_project_validation(self, client):
        """Test that title is required and validated."""
        response = client.post(
            "/api/mission-control/projects",
            json={"title": ""},
        )
        assert response.status_code == 422

    def test_list_projects_empty(self, client):
        """Test listing projects when none exist."""
        response = client.get("/api/mission-control/projects")
        assert response.status_code == 200
        data = response.json()
        assert data["projects"] == []
        assert data["count"] == 0

    def test_list_projects(self, client):
        """Test listing projects."""
        client.post(
            "/api/mission-control/projects",
            json={"title": "Project A"},
        )
        client.post(
            "/api/mission-control/projects",
            json={"title": "Project B"},
        )

        response = client.get("/api/mission-control/projects")
        assert response.status_code == 200
        assert response.json()["count"] == 2

    def test_list_projects_by_status(self, client):
        """Test filtering projects by status."""
        client.post(
            "/api/mission-control/projects",
            json={"title": "Draft"},
        )
        # Create another and approve it
        create_resp = client.post(
            "/api/mission-control/projects",
            json={"title": "Approved"},
        )
        project_id = create_resp.json()["project"]["id"]
        client.post(f"/api/mission-control/projects/{project_id}/approve")

        response = client.get(
            "/api/mission-control/projects",
            params={"status": "draft"},
        )
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert response.json()["projects"][0]["title"] == "Draft"

    def test_get_project(self, client):
        """Test getting a project with tasks and progress."""
        # Create project
        create_resp = client.post(
            "/api/mission-control/projects",
            json={"title": "Detail Project"},
        )
        project_id = create_resp.json()["project"]["id"]

        response = client.get(f"/api/mission-control/projects/{project_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["project"]["title"] == "Detail Project"
        assert "tasks" in data
        assert "progress" in data
        assert data["progress"]["total"] == 0

    def test_get_project_not_found(self, client):
        """Test getting a non-existent project."""
        response = client.get("/api/mission-control/projects/nonexistent")
        assert response.status_code == 404

    def test_update_project(self, client):
        """Test updating a project's fields."""
        create_resp = client.post(
            "/api/mission-control/projects",
            json={"title": "Original Title"},
        )
        project_id = create_resp.json()["project"]["id"]

        response = client.patch(
            f"/api/mission-control/projects/{project_id}",
            json={
                "title": "Updated Title",
                "description": "New description",
                "tags": ["updated"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["project"]["title"] == "Updated Title"
        assert data["project"]["description"] == "New description"
        assert data["project"]["tags"] == ["updated"]

    def test_update_project_status(self, client):
        """Test updating a project's status via PATCH."""
        create_resp = client.post(
            "/api/mission-control/projects",
            json={"title": "Status Project"},
        )
        project_id = create_resp.json()["project"]["id"]

        response = client.patch(
            f"/api/mission-control/projects/{project_id}",
            json={"status": "executing"},
        )
        assert response.status_code == 200
        assert response.json()["project"]["status"] == "executing"

    def test_update_project_not_found(self, client):
        """Test updating a non-existent project."""
        response = client.patch(
            "/api/mission-control/projects/nonexistent",
            json={"title": "Nope"},
        )
        assert response.status_code == 404

    def test_delete_project(self, client):
        """Test deleting a project via API."""
        create_resp = client.post(
            "/api/mission-control/projects",
            json={"title": "Delete Me"},
        )
        project_id = create_resp.json()["project"]["id"]

        response = client.delete(f"/api/mission-control/projects/{project_id}")
        assert response.status_code == 200

        # Verify deleted
        get_resp = client.get(f"/api/mission-control/projects/{project_id}")
        assert get_resp.status_code == 404

    def test_delete_project_not_found(self, client):
        """Test deleting a non-existent project."""
        response = client.delete("/api/mission-control/projects/nonexistent")
        assert response.status_code == 404

    def test_approve_project(self, client):
        """Test approving a project."""
        create_resp = client.post(
            "/api/mission-control/projects",
            json={"title": "Approve Me"},
        )
        project_id = create_resp.json()["project"]["id"]

        response = client.post(f"/api/mission-control/projects/{project_id}/approve")
        assert response.status_code == 200
        assert response.json()["project"]["status"] == "approved"

    def test_approve_project_not_found(self, client):
        """Test approving a non-existent project."""
        response = client.post("/api/mission-control/projects/nonexistent/approve")
        assert response.status_code == 404

    def test_pause_project(self, client):
        """Test pausing a project."""
        create_resp = client.post(
            "/api/mission-control/projects",
            json={"title": "Pause Me"},
        )
        project_id = create_resp.json()["project"]["id"]

        response = client.post(f"/api/mission-control/projects/{project_id}/pause")
        assert response.status_code == 200
        assert response.json()["project"]["status"] == "paused"

    def test_pause_project_not_found(self, client):
        """Test pausing a non-existent project."""
        response = client.post("/api/mission-control/projects/nonexistent/pause")
        assert response.status_code == 404

    def test_resume_project(self, client):
        """Test resuming a project."""
        create_resp = client.post(
            "/api/mission-control/projects",
            json={"title": "Resume Me"},
        )
        project_id = create_resp.json()["project"]["id"]

        # Pause first, then resume
        client.post(f"/api/mission-control/projects/{project_id}/pause")
        response = client.post(f"/api/mission-control/projects/{project_id}/resume")
        assert response.status_code == 200
        assert response.json()["project"]["status"] == "executing"

    def test_resume_project_not_found(self, client):
        """Test resuming a non-existent project."""
        response = client.post("/api/mission-control/projects/nonexistent/resume")
        assert response.status_code == 404
