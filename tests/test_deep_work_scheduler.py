# Tests for Deep Work Dependency Scheduler
# Created: 2026-02-12
#
# Covers:
# - get_ready_tasks: blockers satisfied, excludes running tasks
# - on_task_completed: dispatches agent/human tasks, detects project completion
# - validate_graph: valid DAGs, cycles, missing references (Task + TaskSpec)
# - get_execution_order: level grouping (Task + TaskSpec)

from unittest.mock import AsyncMock, MagicMock

import pytest

from pocketclaw.deep_work.models import Project, ProjectStatus, TaskSpec
from pocketclaw.deep_work.scheduler import DependencyScheduler
from pocketclaw.mission_control.models import Task, TaskStatus

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_manager():
    """Create a mock MissionControlManager."""
    manager = AsyncMock()
    manager.list_tasks = AsyncMock(return_value=[])
    manager.get_project_tasks = AsyncMock(return_value=[])
    manager.get_task = AsyncMock(return_value=None)
    manager.get_project = AsyncMock(return_value=None)
    manager.update_project = AsyncMock()
    return manager


@pytest.fixture
def mock_executor():
    """Create a mock MCTaskExecutor."""
    executor = AsyncMock()
    executor.execute_task_background = AsyncMock()
    executor.is_task_running = MagicMock(return_value=False)
    return executor


@pytest.fixture
def mock_human_router():
    """Create a mock human router."""
    router = AsyncMock()
    router.notify_human_task = AsyncMock()
    router.notify_review_task = AsyncMock()
    return router


@pytest.fixture
def scheduler(mock_manager, mock_executor, mock_human_router):
    """Create a DependencyScheduler with mocked dependencies."""
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
# get_ready_tasks
# ============================================================================


class TestGetReadyTasks:
    async def test_returns_tasks_with_all_blockers_done(self, scheduler, mock_manager):
        """Tasks whose blocked_by are all DONE should be returned."""
        tasks = [
            _make_task("t1", status=TaskStatus.DONE),
            _make_task("t2", status=TaskStatus.DONE),
            _make_task("t3", status=TaskStatus.INBOX, blocked_by=["t1", "t2"]),
        ]
        mock_manager.get_project_tasks.return_value = tasks

        ready = await scheduler.get_ready_tasks("proj-1")

        assert len(ready) == 1
        assert ready[0].id == "t3"

    async def test_excludes_tasks_with_incomplete_blockers(self, scheduler, mock_manager):
        """Tasks with blockers not yet DONE should NOT be returned."""
        tasks = [
            _make_task("t1", status=TaskStatus.DONE),
            _make_task("t2", status=TaskStatus.IN_PROGRESS),
            _make_task("t3", status=TaskStatus.INBOX, blocked_by=["t1", "t2"]),
        ]
        mock_manager.get_project_tasks.return_value = tasks

        ready = await scheduler.get_ready_tasks("proj-1")

        assert len(ready) == 0

    async def test_excludes_already_running_tasks(self, scheduler, mock_manager):
        """Tasks that are IN_PROGRESS should not appear in ready list."""
        tasks = [
            _make_task("t1", status=TaskStatus.IN_PROGRESS),
            _make_task("t2", status=TaskStatus.INBOX),
        ]
        mock_manager.get_project_tasks.return_value = tasks

        ready = await scheduler.get_ready_tasks("proj-1")

        # t2 is INBOX with no blockers, so it's ready; t1 is IN_PROGRESS so excluded
        assert len(ready) == 1
        assert ready[0].id == "t2"

    async def test_returns_tasks_with_no_blockers(self, scheduler, mock_manager):
        """Tasks with empty blocked_by and status INBOX/ASSIGNED are ready."""
        tasks = [
            _make_task("t1", status=TaskStatus.INBOX),
            _make_task("t2", status=TaskStatus.ASSIGNED),
        ]
        mock_manager.get_project_tasks.return_value = tasks

        ready = await scheduler.get_ready_tasks("proj-1")

        assert len(ready) == 2

    async def test_filters_by_project_id(self, scheduler, mock_manager):
        """get_project_tasks returns only tasks for the given project."""
        # get_project_tasks already filters — mock returns only proj-1 tasks
        tasks = [
            _make_task("t1", status=TaskStatus.INBOX, project_id="proj-1"),
        ]
        mock_manager.get_project_tasks.return_value = tasks

        ready = await scheduler.get_ready_tasks("proj-1")

        assert len(ready) == 1
        assert ready[0].id == "t1"
        mock_manager.get_project_tasks.assert_awaited_once_with("proj-1")


# ============================================================================
# on_task_completed
# ============================================================================


class TestOnTaskCompleted:
    async def test_dispatches_agent_task(self, scheduler, mock_manager, mock_executor):
        """When an agent task becomes ready, executor.execute_task_background is called."""
        done_task = _make_task("t1", status=TaskStatus.DONE)
        ready_task = _make_task(
            "t2",
            status=TaskStatus.INBOX,
            blocked_by=["t1"],
            task_type="agent",
            assignee_ids=["agent-1"],
        )

        task_map = {"t1": done_task, "t2": ready_task}
        mock_manager.get_task.side_effect = lambda tid: task_map.get(tid)
        mock_manager.get_project_tasks.return_value = [done_task, ready_task]
        mock_manager.get_project.return_value = None  # Not all done, no project completion

        await scheduler.on_task_completed("t1")

        mock_executor.execute_task_background.assert_awaited_once_with("t2", "agent-1")

    async def test_routes_human_task(
        self, scheduler, mock_manager, mock_executor, mock_human_router
    ):
        """When a human task becomes ready, human_router.notify_human_task is called."""
        done_task = _make_task("t1", status=TaskStatus.DONE)
        human_task = _make_task(
            "t2",
            status=TaskStatus.INBOX,
            blocked_by=["t1"],
            task_type="human",
        )

        task_map = {"t1": done_task, "t2": human_task}
        mock_manager.get_task.side_effect = lambda tid: task_map.get(tid)
        mock_manager.get_project_tasks.return_value = [done_task, human_task]
        mock_manager.get_project.return_value = None

        await scheduler.on_task_completed("t1")

        mock_human_router.notify_human_task.assert_awaited_once_with(human_task)
        mock_executor.execute_task_background.assert_not_awaited()

    async def test_routes_review_task(
        self, scheduler, mock_manager, mock_executor, mock_human_router
    ):
        """When a review task becomes ready, human_router.notify_review_task is called."""
        done_task = _make_task("t1", status=TaskStatus.DONE)
        review_task = _make_task(
            "t2",
            status=TaskStatus.INBOX,
            blocked_by=["t1"],
            task_type="review",
        )

        task_map = {"t1": done_task, "t2": review_task}
        mock_manager.get_task.side_effect = lambda tid: task_map.get(tid)
        mock_manager.get_project_tasks.return_value = [done_task, review_task]
        mock_manager.get_project.return_value = None

        await scheduler.on_task_completed("t1")

        mock_human_router.notify_review_task.assert_awaited_once_with(review_task)

    async def test_detects_project_completion(self, scheduler, mock_manager):
        """When all project tasks are DONE, project status becomes COMPLETED."""
        project = Project(id="proj-1", title="Test Project", status=ProjectStatus.EXECUTING)
        tasks = [
            _make_task("t1", status=TaskStatus.DONE),
            _make_task("t2", status=TaskStatus.DONE),
        ]

        mock_manager.get_task.return_value = tasks[0]
        mock_manager.get_project_tasks.return_value = tasks
        mock_manager.get_project.return_value = project

        await scheduler.on_task_completed("t1")

        mock_manager.update_project.assert_awaited_once()
        updated_project = mock_manager.update_project.call_args[0][0]
        assert updated_project.status == ProjectStatus.COMPLETED
        assert updated_project.completed_at is not None

    async def test_no_dispatch_when_task_has_no_project(self, scheduler, mock_manager):
        """If the completed task has no project_id, do nothing."""
        task = _make_task("t1", status=TaskStatus.DONE, project_id=None)
        mock_manager.get_task.return_value = task

        await scheduler.on_task_completed("t1")

        mock_manager.get_project_tasks.assert_not_awaited()


# ============================================================================
# validate_graph — with Task objects
# ============================================================================


class TestValidateGraphTask:
    def test_valid_linear_chain(self):
        """A->B->C is a valid DAG."""
        tasks = [
            _make_task("A"),
            _make_task("B", blocked_by=["A"]),
            _make_task("C", blocked_by=["B"]),
        ]
        valid, error = DependencyScheduler.validate_graph(tasks)
        assert valid is True
        assert error == ""

    def test_valid_diamond(self):
        """A->{B,C}->D is a valid DAG."""
        tasks = [
            _make_task("A"),
            _make_task("B", blocked_by=["A"]),
            _make_task("C", blocked_by=["A"]),
            _make_task("D", blocked_by=["B", "C"]),
        ]
        valid, error = DependencyScheduler.validate_graph(tasks)
        assert valid is True
        assert error == ""

    def test_detects_simple_cycle(self):
        """A->B->A should be detected as a cycle."""
        tasks = [
            _make_task("A", blocked_by=["B"]),
            _make_task("B", blocked_by=["A"]),
        ]
        valid, error = DependencyScheduler.validate_graph(tasks)
        assert valid is False
        assert "cycle" in error.lower()

    def test_detects_complex_cycle(self):
        """A->B->C->A should be detected as a cycle."""
        tasks = [
            _make_task("A", blocked_by=["C"]),
            _make_task("B", blocked_by=["A"]),
            _make_task("C", blocked_by=["B"]),
        ]
        valid, error = DependencyScheduler.validate_graph(tasks)
        assert valid is False
        assert "cycle" in error.lower()

    def test_detects_nonexistent_reference(self):
        """Reference to non-existent task should fail."""
        tasks = [
            _make_task("A", blocked_by=["Z"]),
        ]
        valid, error = DependencyScheduler.validate_graph(tasks)
        assert valid is False
        assert "non-existent" in error.lower()
        assert "Z" in error

    def test_empty_list_is_valid(self):
        """Empty task list is trivially valid."""
        valid, error = DependencyScheduler.validate_graph([])
        assert valid is True
        assert error == ""


# ============================================================================
# validate_graph — with TaskSpec objects
# ============================================================================


class TestValidateGraphTaskSpec:
    def test_valid_linear_chain(self):
        """A->B->C is a valid DAG with TaskSpec."""
        specs = [
            TaskSpec(key="A"),
            TaskSpec(key="B", blocked_by_keys=["A"]),
            TaskSpec(key="C", blocked_by_keys=["B"]),
        ]
        valid, error = DependencyScheduler.validate_graph(specs)
        assert valid is True
        assert error == ""

    def test_valid_diamond(self):
        """A->{B,C}->D is a valid DAG with TaskSpec."""
        specs = [
            TaskSpec(key="A"),
            TaskSpec(key="B", blocked_by_keys=["A"]),
            TaskSpec(key="C", blocked_by_keys=["A"]),
            TaskSpec(key="D", blocked_by_keys=["B", "C"]),
        ]
        valid, error = DependencyScheduler.validate_graph(specs)
        assert valid is True
        assert error == ""

    def test_detects_cycle(self):
        """Cycle detection works with TaskSpec."""
        specs = [
            TaskSpec(key="A", blocked_by_keys=["B"]),
            TaskSpec(key="B", blocked_by_keys=["A"]),
        ]
        valid, error = DependencyScheduler.validate_graph(specs)
        assert valid is False
        assert "cycle" in error.lower()

    def test_detects_nonexistent_reference(self):
        """Non-existent key reference detected with TaskSpec."""
        specs = [
            TaskSpec(key="A", blocked_by_keys=["missing"]),
        ]
        valid, error = DependencyScheduler.validate_graph(specs)
        assert valid is False
        assert "non-existent" in error.lower()


# ============================================================================
# get_execution_order — with Task objects
# ============================================================================


class TestGetExecutionOrderTask:
    def test_linear_chain(self):
        """A->B->C produces three levels: [A], [B], [C]."""
        tasks = [
            _make_task("A"),
            _make_task("B", blocked_by=["A"]),
            _make_task("C", blocked_by=["B"]),
        ]
        levels = DependencyScheduler.get_execution_order(tasks)
        assert len(levels) == 3
        assert levels[0] == ["A"]
        assert levels[1] == ["B"]
        assert levels[2] == ["C"]

    def test_diamond(self):
        """A->{B,C}->D produces: [A], [B,C], [D]."""
        tasks = [
            _make_task("A"),
            _make_task("B", blocked_by=["A"]),
            _make_task("C", blocked_by=["A"]),
            _make_task("D", blocked_by=["B", "C"]),
        ]
        levels = DependencyScheduler.get_execution_order(tasks)
        assert len(levels) == 3
        assert levels[0] == ["A"]
        assert sorted(levels[1]) == ["B", "C"]
        assert levels[2] == ["D"]

    def test_tasks_with_no_deps(self):
        """All tasks with no deps are at level 0."""
        tasks = [
            _make_task("A"),
            _make_task("B"),
            _make_task("C"),
        ]
        levels = DependencyScheduler.get_execution_order(tasks)
        assert len(levels) == 1
        assert sorted(levels[0]) == ["A", "B", "C"]

    def test_empty_list(self):
        """Empty task list returns empty levels."""
        levels = DependencyScheduler.get_execution_order([])
        assert levels == []


# ============================================================================
# get_execution_order — with TaskSpec objects
# ============================================================================


class TestGetExecutionOrderTaskSpec:
    def test_linear_chain(self):
        """A->B->C with TaskSpec."""
        specs = [
            TaskSpec(key="A"),
            TaskSpec(key="B", blocked_by_keys=["A"]),
            TaskSpec(key="C", blocked_by_keys=["B"]),
        ]
        levels = DependencyScheduler.get_execution_order(specs)
        assert len(levels) == 3
        assert levels[0] == ["A"]
        assert levels[1] == ["B"]
        assert levels[2] == ["C"]

    def test_diamond(self):
        """A->{B,C}->D with TaskSpec."""
        specs = [
            TaskSpec(key="A"),
            TaskSpec(key="B", blocked_by_keys=["A"]),
            TaskSpec(key="C", blocked_by_keys=["A"]),
            TaskSpec(key="D", blocked_by_keys=["B", "C"]),
        ]
        levels = DependencyScheduler.get_execution_order(specs)
        assert len(levels) == 3
        assert levels[0] == ["A"]
        assert sorted(levels[1]) == ["B", "C"]
        assert levels[2] == ["D"]
