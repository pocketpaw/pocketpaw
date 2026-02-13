# Tests for execution levels in Deep Work plan API
# Created: 2026-02-12
#
# Covers:
# - GET /projects/{id}/plan returns execution_levels and task_level_map
# - Correct level grouping for linear, diamond, and independent task graphs

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
from pocketclaw.mission_control.models import TaskStatus

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_store_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def test_app(temp_store_path, monkeypatch):
    """Create test FastAPI app with both MC and Deep Work routers."""
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
def manager(test_app, monkeypatch):
    import pocketclaw.mission_control.manager as manager_module

    return manager_module._manager_instance


# ============================================================================
# Tests
# ============================================================================


class TestPlanExecutionLevels:
    """Test that GET /projects/{id}/plan returns execution_levels and task_level_map."""

    @pytest.mark.asyncio
    async def test_linear_chain_levels(self, client, manager):
        """A->B->C should produce 3 execution levels."""
        project = await manager.create_project(title="Linear Project")

        # Create tasks: A (no deps), B (blocked by A), C (blocked by B)
        t_a = await manager.create_task(title="Task A")
        t_a.project_id = project.id
        await manager._store.save_task(t_a)

        t_b = await manager.create_task(title="Task B")
        t_b.project_id = project.id
        t_b.blocked_by = [t_a.id]
        await manager._store.save_task(t_b)

        t_c = await manager.create_task(title="Task C")
        t_c.project_id = project.id
        t_c.blocked_by = [t_b.id]
        await manager._store.save_task(t_c)

        response = client.get(f"/api/deep-work/projects/{project.id}/plan")
        assert response.status_code == 200
        data = response.json()

        assert "execution_levels" in data
        assert "task_level_map" in data

        levels = data["execution_levels"]
        level_map = data["task_level_map"]

        assert len(levels) == 3
        assert levels[0] == [t_a.id]
        assert levels[1] == [t_b.id]
        assert levels[2] == [t_c.id]

        assert level_map[t_a.id] == 0
        assert level_map[t_b.id] == 1
        assert level_map[t_c.id] == 2

    @pytest.mark.asyncio
    async def test_diamond_levels(self, client, manager):
        """A->{B,C}->D should produce 3 levels: [A], [B,C], [D]."""
        project = await manager.create_project(title="Diamond Project")

        t_a = await manager.create_task(title="Task A")
        t_a.project_id = project.id
        await manager._store.save_task(t_a)

        t_b = await manager.create_task(title="Task B")
        t_b.project_id = project.id
        t_b.blocked_by = [t_a.id]
        await manager._store.save_task(t_b)

        t_c = await manager.create_task(title="Task C")
        t_c.project_id = project.id
        t_c.blocked_by = [t_a.id]
        await manager._store.save_task(t_c)

        t_d = await manager.create_task(title="Task D")
        t_d.project_id = project.id
        t_d.blocked_by = [t_b.id, t_c.id]
        await manager._store.save_task(t_d)

        response = client.get(f"/api/deep-work/projects/{project.id}/plan")
        data = response.json()

        levels = data["execution_levels"]
        level_map = data["task_level_map"]

        assert len(levels) == 3
        assert levels[0] == [t_a.id]
        assert sorted(levels[1]) == sorted([t_b.id, t_c.id])
        assert levels[2] == [t_d.id]

        assert level_map[t_d.id] == 2

    @pytest.mark.asyncio
    async def test_independent_tasks_single_level(self, client, manager):
        """Tasks with no deps should all be at level 0."""
        project = await manager.create_project(title="Independent Project")

        tasks = []
        for name in ["A", "B", "C"]:
            t = await manager.create_task(title=f"Task {name}")
            t.project_id = project.id
            await manager._store.save_task(t)
            tasks.append(t)

        response = client.get(f"/api/deep-work/projects/{project.id}/plan")
        data = response.json()

        levels = data["execution_levels"]
        assert len(levels) == 1
        assert sorted(levels[0]) == sorted([t.id for t in tasks])

    @pytest.mark.asyncio
    async def test_empty_project_levels(self, client, manager):
        """Project with no tasks should return empty execution_levels."""
        project = await manager.create_project(title="Empty Project")

        response = client.get(f"/api/deep-work/projects/{project.id}/plan")
        data = response.json()

        assert data["execution_levels"] == []
        assert data["task_level_map"] == {}

    @pytest.mark.asyncio
    async def test_progress_includes_skipped(self, client, manager):
        """Plan API progress should include skipped count."""
        project = await manager.create_project(title="Skipped Progress")

        t1 = await manager.create_task(title="Done Task")
        t1.project_id = project.id
        t1.status = TaskStatus.DONE
        await manager._store.save_task(t1)

        t2 = await manager.create_task(title="Skipped Task")
        t2.project_id = project.id
        t2.status = TaskStatus.SKIPPED
        await manager._store.save_task(t2)

        t3 = await manager.create_task(title="Pending Task")
        t3.project_id = project.id
        t3.status = TaskStatus.INBOX
        await manager._store.save_task(t3)

        response = client.get(f"/api/deep-work/projects/{project.id}/plan")
        data = response.json()

        progress = data["progress"]
        assert progress["completed"] == 1
        assert progress["skipped"] == 1
        assert progress["total"] == 3
        # (1 + 1) / 3 * 100 = 66.7
        assert abs(progress["percent"] - 66.7) < 0.1
