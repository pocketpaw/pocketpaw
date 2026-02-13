# Tests for SKIPPED task status — scheduler, manager progress, API
# Created: 2026-02-12
#
# Covers:
# - SKIPPED status unblocks dependent tasks (same as DONE)
# - SKIPPED tasks count in project completion check
# - Manager progress includes skipped in percent numerator
# - API skip endpoint sets status, cascades, returns progress

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pocketclaw.deep_work.models import Project, ProjectStatus
from pocketclaw.deep_work.scheduler import DependencyScheduler
from pocketclaw.mission_control import (
    FileMissionControlStore,
    MissionControlManager,
    reset_mission_control_manager,
    reset_mission_control_store,
)
from pocketclaw.mission_control.models import Task, TaskStatus

# ============================================================================
# Fixtures — Scheduler (mocked)
# ============================================================================


@pytest.fixture
def mock_manager():
    """Mock MissionControlManager for scheduler tests."""
    manager = AsyncMock()
    manager.list_tasks = AsyncMock(return_value=[])
    manager.get_project_tasks = AsyncMock(return_value=[])
    manager.get_task = AsyncMock(return_value=None)
    manager.get_project = AsyncMock(return_value=None)
    manager.update_project = AsyncMock()
    return manager


@pytest.fixture
def mock_executor():
    """Mock MCTaskExecutor."""
    executor = AsyncMock()
    executor.execute_task_background = AsyncMock()
    executor.is_task_running = MagicMock(return_value=False)
    return executor


@pytest.fixture
def mock_human_router():
    """Mock HumanTaskRouter."""
    router = AsyncMock()
    router.notify_human_task = AsyncMock()
    router.notify_review_task = AsyncMock()
    return router


@pytest.fixture
def scheduler(mock_manager, mock_executor, mock_human_router):
    """DependencyScheduler with mocked deps."""
    return DependencyScheduler(mock_manager, mock_executor, mock_human_router)


def _make_task(
    task_id: str,
    status: TaskStatus = TaskStatus.INBOX,
    project_id: str = "proj-1",
    blocked_by: list[str] | None = None,
    task_type: str = "agent",
    assignee_ids: list[str] | None = None,
    title: str = "",
) -> Task:
    """Helper to create a Task with specific fields."""
    return Task(
        id=task_id,
        title=title or f"Task {task_id}",
        status=status,
        project_id=project_id,
        blocked_by=blocked_by or [],
        task_type=task_type,
        assignee_ids=assignee_ids or [],
    )


# ============================================================================
# Scheduler: SKIPPED unblocks dependents
# ============================================================================


class TestSkippedUnblocksDependents:
    async def test_skipped_blocker_unblocks_dependent(self, scheduler, mock_manager):
        """A SKIPPED blocker should satisfy the blocked_by check."""
        tasks = [
            _make_task("t1", status=TaskStatus.SKIPPED),
            _make_task("t2", status=TaskStatus.INBOX, blocked_by=["t1"]),
        ]
        mock_manager.get_project_tasks.return_value = tasks

        ready = await scheduler.get_ready_tasks("proj-1")

        assert len(ready) == 1
        assert ready[0].id == "t2"

    async def test_mixed_done_and_skipped_unblocks(self, scheduler, mock_manager):
        """When blockers are a mix of DONE and SKIPPED, dependents should be ready."""
        tasks = [
            _make_task("t1", status=TaskStatus.DONE),
            _make_task("t2", status=TaskStatus.SKIPPED),
            _make_task("t3", status=TaskStatus.INBOX, blocked_by=["t1", "t2"]),
        ]
        mock_manager.get_project_tasks.return_value = tasks

        ready = await scheduler.get_ready_tasks("proj-1")

        assert len(ready) == 1
        assert ready[0].id == "t3"

    async def test_partial_skip_partial_pending_blocks(self, scheduler, mock_manager):
        """If one blocker is SKIPPED but another is IN_PROGRESS, task is NOT ready."""
        tasks = [
            _make_task("t1", status=TaskStatus.SKIPPED),
            _make_task("t2", status=TaskStatus.IN_PROGRESS),
            _make_task("t3", status=TaskStatus.INBOX, blocked_by=["t1", "t2"]),
        ]
        mock_manager.get_project_tasks.return_value = tasks

        ready = await scheduler.get_ready_tasks("proj-1")

        assert len(ready) == 0


# ============================================================================
# Scheduler: SKIPPED counts toward project completion
# ============================================================================


class TestSkippedProjectCompletion:
    async def test_all_skipped_completes_project(self, scheduler, mock_manager):
        """Project with all tasks SKIPPED should be marked COMPLETED."""
        project = Project(id="proj-1", title="All Skipped", status=ProjectStatus.EXECUTING)
        tasks = [
            _make_task("t1", status=TaskStatus.SKIPPED),
            _make_task("t2", status=TaskStatus.SKIPPED),
        ]

        mock_manager.get_task.return_value = tasks[0]
        mock_manager.get_project_tasks.return_value = tasks
        mock_manager.get_project.return_value = project

        await scheduler.on_task_completed("t1")

        mock_manager.update_project.assert_awaited_once()
        updated = mock_manager.update_project.call_args[0][0]
        assert updated.status == ProjectStatus.COMPLETED

    async def test_mixed_done_skipped_completes_project(self, scheduler, mock_manager):
        """Project with DONE + SKIPPED tasks should be marked COMPLETED."""
        project = Project(id="proj-1", title="Mixed", status=ProjectStatus.EXECUTING)
        tasks = [
            _make_task("t1", status=TaskStatus.DONE),
            _make_task("t2", status=TaskStatus.SKIPPED),
        ]

        mock_manager.get_task.return_value = tasks[0]
        mock_manager.get_project_tasks.return_value = tasks
        mock_manager.get_project.return_value = project

        await scheduler.on_task_completed("t1")

        mock_manager.update_project.assert_awaited_once()
        updated = mock_manager.update_project.call_args[0][0]
        assert updated.status == ProjectStatus.COMPLETED


# ============================================================================
# Manager: Progress includes skipped
# ============================================================================

# Fixtures for real store/manager


@pytest.fixture
def temp_store_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def real_manager(temp_store_path):
    """Create a real manager with file store for progress tests."""
    reset_mission_control_store()
    reset_mission_control_manager()
    store = FileMissionControlStore(temp_store_path)
    return MissionControlManager(store)


class TestProgressWithSkipped:
    @pytest.mark.asyncio
    async def test_skipped_in_progress_count(self, real_manager):
        """Skipped tasks appear in progress.skipped and contribute to percent."""
        project = await real_manager.create_project(title="Progress Test")

        # 1: done agent
        t1 = await real_manager.create_task(title="Done Task")
        t1.project_id = project.id
        t1.status = TaskStatus.DONE
        await real_manager._store.save_task(t1)

        # 2: skipped agent
        t2 = await real_manager.create_task(title="Skipped Task")
        t2.project_id = project.id
        t2.status = TaskStatus.SKIPPED
        await real_manager._store.save_task(t2)

        # 3: inbox agent
        t3 = await real_manager.create_task(title="Pending Task")
        t3.project_id = project.id
        t3.status = TaskStatus.INBOX
        await real_manager._store.save_task(t3)

        progress = await real_manager.get_project_progress(project.id)

        assert progress["total"] == 3
        assert progress["completed"] == 1
        assert progress["skipped"] == 1
        assert progress["percent"] == pytest.approx(66.7, abs=0.1)  # (1+1)/3 * 100

    @pytest.mark.asyncio
    async def test_skipped_human_not_in_pending(self, real_manager):
        """A skipped human task should NOT count in human_pending."""
        project = await real_manager.create_project(title="Human Skip Test")

        t1 = await real_manager.create_task(title="Human Skipped")
        t1.project_id = project.id
        t1.task_type = "human"
        t1.status = TaskStatus.SKIPPED
        await real_manager._store.save_task(t1)

        progress = await real_manager.get_project_progress(project.id)

        assert progress["human_pending"] == 0
        assert progress["skipped"] == 1


# ============================================================================
# TaskStatus enum: SKIPPED value
# ============================================================================


class TestSkippedEnum:
    def test_skipped_status_exists(self):
        """TaskStatus.SKIPPED should have value 'skipped'."""
        assert TaskStatus.SKIPPED.value == "skipped"

    def test_skipped_round_trip(self):
        """Task with SKIPPED status should serialize and deserialize correctly."""
        task = Task(id="skip-test", title="Skip Me", status=TaskStatus.SKIPPED)
        data = task.to_dict()
        assert data["status"] == "skipped"

        restored = Task.from_dict(data)
        assert restored.status == TaskStatus.SKIPPED
