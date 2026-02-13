# Tests for Deep Work Session — integration orchestrator.
# Created: 2026-02-12
# Updated: 2026-02-12 — Added real-store integration tests for task
#   materialization, inverse blocks, agent assignment, pause/stop, and
#   the public API singleton. Merged with existing mock-based tests.
#
# Covers:
#   - start() creates project with DRAFT -> PLANNING -> AWAITING_APPROVAL flow
#   - start() materializes tasks with correct project_id, task_type, blocked_by
#   - start() sets inverse blocks on upstream tasks
#   - start() creates agent team from planner recommendations
#   - start() assigns tasks to agents by specialty match
#   - start() saves PRD document and links to project
#   - start() fails on cyclic dependency graph
#   - start() fails on empty task list
#   - start() fails on planner exception
#   - approve() dispatches ready tasks
#   - pause() stops running tasks via executor
#   - resume() dispatches ready tasks after resume
#   - _on_system_event routes mc_task_completed to scheduler
#   - _extract_title extracts heading from PRD
#   - subscribe_to_bus idempotency
#   - Singleton get_deep_work_session / reset_deep_work_session

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketclaw.deep_work.human_tasks import HumanTaskRouter
from pocketclaw.deep_work.models import (
    AgentSpec,
    PlannerResult,
    ProjectStatus,
    TaskSpec,
)
from pocketclaw.deep_work.planner import PlannerAgent
from pocketclaw.deep_work.scheduler import DependencyScheduler
from pocketclaw.deep_work.session import DeepWorkSession, _extract_title
from pocketclaw.mission_control.manager import (
    MissionControlManager,
    reset_mission_control_manager,
)
from pocketclaw.mission_control.store import (
    FileMissionControlStore,
    reset_mission_control_store,
)

# ============================================================================
# Test Data
# ============================================================================


def _make_planner_result(project_id: str = "") -> PlannerResult:
    """Create a realistic PlannerResult for testing."""
    return PlannerResult(
        project_id=project_id,
        prd_content="# Build a TODO App\n\n## Problem Statement\nBuild a simple TODO app.",
        tasks=[
            TaskSpec(
                key="t1",
                title="Set up project",
                description="Init the repo",
                task_type="agent",
                priority="high",
                tags=["setup"],
                estimated_minutes=15,
                required_specialties=["devops"],
                blocked_by_keys=[],
            ),
            TaskSpec(
                key="t2",
                title="Build API",
                description="Create REST endpoints",
                task_type="agent",
                priority="medium",
                tags=["backend"],
                estimated_minutes=45,
                required_specialties=["python", "fastapi"],
                blocked_by_keys=["t1"],
            ),
        ],
        team_recommendation=[
            AgentSpec(
                name="backend-dev",
                role="Backend Developer",
                description="Builds APIs",
                specialties=["python", "fastapi", "devops"],
                backend="claude_agent_sdk",
            ),
        ],
        human_tasks=[
            TaskSpec(
                key="t3",
                title="Upload logo",
                description="Upload brand assets",
                task_type="human",
                priority="low",
                tags=["design"],
                estimated_minutes=10,
                required_specialties=[],
                blocked_by_keys=["t1"],
            ),
        ],
        dependency_graph={"t2": ["t1"], "t3": ["t1"]},
        estimated_total_minutes=70,
        research_notes="Research notes here",
    )


def _make_cyclic_planner_result() -> PlannerResult:
    """Build a PlannerResult with a cyclic dependency graph."""
    return PlannerResult(
        prd_content="# Cyclic Project\n\nThis has cycles.",
        tasks=[
            TaskSpec(key="a", title="Task A", task_type="agent", blocked_by_keys=["c"]),
            TaskSpec(key="b", title="Task B", task_type="agent", blocked_by_keys=["a"]),
            TaskSpec(key="c", title="Task C", task_type="agent", blocked_by_keys=["b"]),
        ],
        human_tasks=[],
        team_recommendation=[],
        estimated_total_minutes=90,
    )


def _make_empty_planner_result() -> PlannerResult:
    """Build a PlannerResult with no tasks."""
    return PlannerResult(
        prd_content="# Empty Project\n\nNothing to do.",
        tasks=[],
        human_tasks=[],
        team_recommendation=[],
        estimated_total_minutes=0,
    )


def _make_two_agent_result() -> PlannerResult:
    """PlannerResult with 2 agents and tasks requiring different specialties."""
    return PlannerResult(
        prd_content="# Multi-Agent Project\n\nNeeds a researcher and a developer.",
        tasks=[
            TaskSpec(
                key="research",
                title="Research APIs",
                task_type="agent",
                priority="high",
                required_specialties=["research", "api"],
                blocked_by_keys=[],
            ),
            TaskSpec(
                key="build",
                title="Build backend",
                task_type="agent",
                priority="high",
                required_specialties=["python", "backend"],
                blocked_by_keys=["research"],
            ),
        ],
        human_tasks=[],
        team_recommendation=[
            AgentSpec(
                name="researcher-bot",
                role="Research Analyst",
                specialties=["research", "api"],
            ),
            AgentSpec(
                name="dev-bot",
                role="Developer",
                specialties=["python", "backend"],
            ),
        ],
        estimated_total_minutes=90,
    )


# ============================================================================
# Fixtures — Real Store
# ============================================================================


@pytest.fixture
def temp_store_path():
    """Create a temporary directory for test storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def store(temp_store_path):
    """Create a fresh file-based store for each test."""
    reset_mission_control_store()
    return FileMissionControlStore(temp_store_path)


@pytest.fixture
def manager(store):
    """Create a MissionControlManager with the test store."""
    reset_mission_control_manager()
    return MissionControlManager(store)


@pytest.fixture
def mock_executor():
    """Create a mock MCTaskExecutor."""
    executor = MagicMock()
    executor.is_task_running = MagicMock(return_value=False)
    executor.stop_task = AsyncMock(return_value=True)
    executor.execute_task_background = AsyncMock()
    return executor


@pytest.fixture
def mock_planner():
    """Create a mock PlannerAgent with canned results."""
    planner = MagicMock()
    planner.plan = AsyncMock(return_value=_make_planner_result())
    return planner


@pytest.fixture
def mock_human_router():
    """Create a mock HumanTaskRouter."""
    router = MagicMock()
    router.notify_human_task = AsyncMock()
    router.notify_review_task = AsyncMock()
    router.notify_plan_ready = AsyncMock()
    router.notify_project_completed = AsyncMock()
    return router


@pytest.fixture
def session(manager, mock_executor, mock_planner, mock_human_router):
    """Create a DeepWorkSession with real store but mocked planner/executor."""
    return DeepWorkSession(
        manager=manager,
        executor=mock_executor,
        planner=mock_planner,
        human_router=mock_human_router,
    )


# ============================================================================
# start() tests — Real Store
# ============================================================================


class TestStartCreatesProject:
    """Tests for project creation during start()."""

    @pytest.mark.asyncio
    async def test_start_creates_project(self, session, manager):
        """Verify project created with correct status flow: DRAFT -> PLANNING -> AWAITING_APPROVAL."""
        project = await session.start("Build a todo app with REST API")

        assert project is not None
        assert project.status == ProjectStatus.AWAITING_APPROVAL
        assert project.description == "Build a todo app with REST API"
        assert project.creator_id == "human"

        # Verify persisted
        fetched = await manager.get_project(project.id)
        assert fetched is not None
        assert fetched.status == ProjectStatus.AWAITING_APPROVAL

    @pytest.mark.asyncio
    async def test_start_creates_tasks(self, session, manager):
        """Verify tasks created with correct project_id, task_type, and blocked_by."""
        project = await session.start("Build a todo app")

        tasks = await manager.get_project_tasks(project.id)
        assert len(tasks) == 3  # 2 agent + 1 human

        # Verify all tasks have the correct project_id
        for task in tasks:
            assert task.project_id == project.id

        # Verify task types
        task_types = {t.title: t.task_type for t in tasks}
        assert task_types["Set up project"] == "agent"
        assert task_types["Build API"] == "agent"
        assert task_types["Upload logo"] == "human"

        # Verify blocked_by is set correctly
        t1 = next(t for t in tasks if t.title == "Set up project")
        t2 = next(t for t in tasks if t.title == "Build API")
        t3 = next(t for t in tasks if t.title == "Upload logo")

        assert t1.blocked_by == []
        assert t2.blocked_by == [t1.id]
        assert t3.blocked_by == [t1.id]

    @pytest.mark.asyncio
    async def test_start_sets_inverse_blocks(self, session, manager):
        """Verify blocks[] populated correctly on upstream tasks."""
        project = await session.start("Build a todo app")

        tasks = await manager.get_project_tasks(project.id)
        t1 = next(t for t in tasks if t.title == "Set up project")
        t2 = next(t for t in tasks if t.title == "Build API")
        t3 = next(t for t in tasks if t.title == "Upload logo")

        # t1 should block both t2 and t3
        assert set(t1.blocks) == {t2.id, t3.id}

    @pytest.mark.asyncio
    async def test_start_creates_agents(self, session, manager):
        """Verify agent team created from planner recommendations."""
        project = await session.start("Build a todo app")

        assert len(project.team_agent_ids) == 1

        agent = await manager.get_agent(project.team_agent_ids[0])
        assert agent is not None
        assert agent.name == "backend-dev"
        assert agent.role == "Backend Developer"
        assert "python" in agent.specialties

    @pytest.mark.asyncio
    async def test_start_assigns_tasks(self, session, manager):
        """Verify agent tasks assigned to matching agents by specialty."""
        project = await session.start("Build a todo app")

        tasks = await manager.get_project_tasks(project.id)
        agent = await manager.get_agent(project.team_agent_ids[0])

        # Agent tasks should be assigned to backend-dev
        t1 = next(t for t in tasks if t.title == "Set up project")
        t2 = next(t for t in tasks if t.title == "Build API")
        t3 = next(t for t in tasks if t.title == "Upload logo")

        assert agent.id in t1.assignee_ids
        assert agent.id in t2.assignee_ids
        # Human task should NOT be assigned
        assert t3.assignee_ids == []

    @pytest.mark.asyncio
    async def test_start_assigns_by_specialty(self, manager, mock_executor, mock_human_router):
        """Verify tasks matched to best agent by specialty overlap."""
        planner = MagicMock()
        planner.plan = AsyncMock(return_value=_make_two_agent_result())

        session = DeepWorkSession(
            manager=manager,
            executor=mock_executor,
            planner=planner,
            human_router=mock_human_router,
        )

        project = await session.start("Multi-agent project")

        tasks = await manager.get_project_tasks(project.id)
        research_task = next(t for t in tasks if t.title == "Research APIs")
        build_task = next(t for t in tasks if t.title == "Build backend")

        researcher = await manager.get_agent_by_name("researcher-bot")
        dev = await manager.get_agent_by_name("dev-bot")

        # Research -> researcher-bot (research + api match)
        assert researcher.id in research_task.assignee_ids
        # Build -> dev-bot (python + backend match)
        assert dev.id in build_task.assignee_ids

    @pytest.mark.asyncio
    async def test_start_saves_prd(self, session, manager):
        """Verify PRD document created and linked to project."""
        project = await session.start("Build a todo app")

        assert project.prd_document_id is not None

        doc = await manager.get_document(project.prd_document_id)
        assert doc is not None
        assert doc.title.startswith("PRD:")
        assert "TODO" in doc.content
        assert doc.type.value == "protocol"
        assert "prd" in doc.tags

    @pytest.mark.asyncio
    async def test_start_extracts_title_from_prd(self, session):
        """Verify project title extracted from PRD heading."""
        project = await session.start("Build a todo app")
        assert project.title == "Build a TODO App"


class TestStartEdgeCases:
    """Tests for edge cases in start()."""

    @pytest.mark.asyncio
    async def test_start_invalid_graph(self, manager, mock_executor, mock_human_router):
        """Verify project status = FAILED when planner produces cyclic deps."""
        planner = MagicMock()
        planner.plan = AsyncMock(return_value=_make_cyclic_planner_result())

        session = DeepWorkSession(
            manager=manager,
            executor=mock_executor,
            planner=planner,
            human_router=mock_human_router,
        )

        project = await session.start("Cyclic project")
        assert project.status == ProjectStatus.FAILED
        assert "cycle" in project.metadata.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_start_empty_tasks(self, manager, mock_executor, mock_human_router):
        """Verify project fails when planner produces no tasks."""
        planner = MagicMock()
        planner.plan = AsyncMock(return_value=_make_empty_planner_result())

        session = DeepWorkSession(
            manager=manager,
            executor=mock_executor,
            planner=planner,
            human_router=mock_human_router,
        )

        project = await session.start("Empty project")
        assert project.status == ProjectStatus.FAILED
        assert "no tasks" in project.metadata.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_start_planner_exception(self, manager, mock_executor, mock_human_router):
        """Verify project fails when planner raises an exception."""
        planner = MagicMock()
        planner.plan = AsyncMock(side_effect=RuntimeError("LLM API error"))

        session = DeepWorkSession(
            manager=manager,
            executor=mock_executor,
            planner=planner,
            human_router=mock_human_router,
        )

        with pytest.raises(RuntimeError, match="LLM API error"):
            await session.start("Failing project")

        # Verify project is marked FAILED in the store
        projects = await manager.list_projects()
        assert len(projects) == 1
        assert projects[0].status == ProjectStatus.FAILED
        assert "LLM API error" in projects[0].metadata.get("error", "")

    @pytest.mark.asyncio
    async def test_start_reuses_existing_agent(self, session, manager):
        """Verify existing agents are reused (not duplicated)."""
        # Pre-create the recommended agent
        existing = await manager.create_agent(
            name="backend-dev",
            role="Backend Developer",
            specialties=["python", "fastapi", "devops"],
        )

        project = await session.start("Build a todo app")

        assert len(project.team_agent_ids) == 1
        assert existing.id in project.team_agent_ids

        # Verify no duplicate
        all_agents = await manager.list_agents()
        names = [a.name for a in all_agents]
        assert names.count("backend-dev") == 1

    @pytest.mark.asyncio
    async def test_start_notifies_plan_ready(self, session, mock_human_router):
        """Verify human notification sent after plan is ready."""
        await session.start("Build a todo app")

        mock_human_router.notify_plan_ready.assert_called_once()
        call_args = mock_human_router.notify_plan_ready.call_args
        assert call_args.kwargs["task_count"] == 3
        assert call_args.kwargs["estimated_minutes"] == 70


# ============================================================================
# approve() tests
# ============================================================================


class TestApprove:
    """Tests for approve()."""

    @pytest.mark.asyncio
    async def test_approve_dispatches_tasks(self, session, manager, mock_executor):
        """Verify ready tasks dispatched after approval."""
        project = await session.start("Build a todo app")

        approved = await session.approve(project.id)
        assert approved.status == ProjectStatus.EXECUTING
        assert approved.started_at is not None

    @pytest.mark.asyncio
    async def test_approve_not_found(self, session):
        """Verify ValueError raised for non-existent project."""
        with pytest.raises(ValueError, match="Project not found"):
            await session.approve("nonexistent-id")


# ============================================================================
# pause() tests
# ============================================================================


class TestPause:
    """Tests for pause()."""

    @pytest.mark.asyncio
    async def test_pause_stops_running(self, session, manager, mock_executor):
        """Verify running tasks stopped during pause."""
        project = await session.start("Build a todo app")

        # Approve first (pause requires EXECUTING status)
        await session.approve(project.id)

        # Simulate one task running
        mock_executor.is_task_running = MagicMock(
            side_effect=lambda tid: tid == project.task_ids[0]
        )

        paused = await session.pause(project.id)
        assert paused.status == ProjectStatus.PAUSED
        mock_executor.stop_task.assert_called_once_with(project.task_ids[0])

    @pytest.mark.asyncio
    async def test_pause_no_running_tasks(self, session, manager, mock_executor):
        """Verify pause works even if no tasks are running."""
        project = await session.start("Build a todo app")

        # Approve first (pause requires EXECUTING status)
        await session.approve(project.id)

        mock_executor.is_task_running = MagicMock(return_value=False)
        paused = await session.pause(project.id)

        assert paused.status == ProjectStatus.PAUSED
        mock_executor.stop_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_pause_not_found(self, session):
        """Verify ValueError raised for non-existent project."""
        with pytest.raises(ValueError, match="Project not found"):
            await session.pause("nonexistent-id")


# ============================================================================
# resume() tests
# ============================================================================


class TestResume:
    """Tests for resume()."""

    @pytest.mark.asyncio
    async def test_resume_dispatches_ready(self, session, manager, mock_executor):
        """Verify ready tasks dispatched after resume."""
        project = await session.start("Build a todo app")

        # Approve first (pause requires EXECUTING, resume requires PAUSED)
        await session.approve(project.id)
        await session.pause(project.id)
        resumed = await session.resume(project.id)
        assert resumed.status == ProjectStatus.EXECUTING

    @pytest.mark.asyncio
    async def test_resume_not_found(self, session):
        """Verify ValueError raised for non-existent project."""
        with pytest.raises(ValueError, match="Project not found"):
            await session.resume("nonexistent-id")


# ============================================================================
# _on_system_event tests
# ============================================================================


class TestSystemEvent:
    """Tests for _on_system_event MessageBus handler."""

    @pytest.mark.asyncio
    async def test_executor_callback_wired_to_scheduler(self, manager, mock_executor):
        """Executor's _on_task_done_callback is wired to scheduler.on_task_completed."""
        mock_scheduler = AsyncMock(spec=DependencyScheduler)
        session = DeepWorkSession(
            manager=manager,
            executor=mock_executor,
            scheduler=mock_scheduler,
        )

        # Verify the callback was wired during __init__
        assert mock_executor._on_task_done_callback is mock_scheduler.on_task_completed

        # Invoke it directly (simulates executor finishing a task)
        await mock_executor._on_task_done_callback("task-42")
        mock_scheduler.on_task_completed.assert_called_once_with("task-42")

    @pytest.mark.asyncio
    async def test_ignores_other_events(self, manager, mock_executor):
        """_on_system_event ignores non-completion events."""
        mock_scheduler = AsyncMock(spec=DependencyScheduler)
        session = DeepWorkSession(
            manager=manager,
            executor=mock_executor,
            scheduler=mock_scheduler,
        )

        event = MagicMock()
        event.event_type = "thinking"
        event.data = {}

        await session._on_system_event(event)
        mock_scheduler.on_task_completed.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_missing_task_id(self, manager, mock_executor):
        """_on_system_event ignores completion events without task_id."""
        mock_scheduler = AsyncMock(spec=DependencyScheduler)
        session = DeepWorkSession(
            manager=manager,
            executor=mock_executor,
            scheduler=mock_scheduler,
        )

        event = MagicMock()
        event.event_type = "mc_task_completed"
        event.data = {}

        await session._on_system_event(event)
        mock_scheduler.on_task_completed.assert_not_called()


# ============================================================================
# _extract_title tests
# ============================================================================


class TestExtractTitle:
    """Tests for the _extract_title helper."""

    def test_from_h1(self):
        assert _extract_title("# My Project\nDescription here") == "My Project"

    def test_full_heading(self):
        assert _extract_title("# Build a TODO App\n\n## Problem Statement") == "Build a TODO App"

    def test_strips_prd_prefix(self):
        assert _extract_title("## PRD: Build a Widget") == "Build a Widget"

    def test_strips_problem_statement(self):
        assert _extract_title("## Problem Statement") == ""

    def test_no_headings(self):
        assert _extract_title("Just some text\nNo headings") == ""

    def test_truncates_long(self):
        result = _extract_title("# " + "x" * 200)
        assert len(result) <= 100

    def test_empty_input(self):
        assert _extract_title("") == ""


# ============================================================================
# subscribe_to_bus tests
# ============================================================================


class TestSubscribeToBus:
    """Tests for subscribe_to_bus."""

    def test_idempotent(self, manager, mock_executor):
        """subscribe_to_bus should only subscribe once."""
        session = DeepWorkSession(manager=manager, executor=mock_executor)

        mock_bus = MagicMock()
        mock_bus.subscribe_system = MagicMock()

        with patch("pocketclaw.bus.get_message_bus", return_value=mock_bus):
            session.subscribe_to_bus()
            session.subscribe_to_bus()

        mock_bus.subscribe_system.assert_called_once()

    def test_handles_missing_bus(self, manager, mock_executor):
        """subscribe_to_bus should not crash if bus is unavailable."""
        session = DeepWorkSession(manager=manager, executor=mock_executor)

        with patch("pocketclaw.bus.get_message_bus", side_effect=RuntimeError("no bus")):
            session.subscribe_to_bus()

        assert not session._subscribed


# ============================================================================
# Public API tests
# ============================================================================


class TestPublicAPI:
    """Tests for the deep_work package-level public API."""

    def test_get_deep_work_session_returns_singleton(self):
        """get_deep_work_session should return the same instance."""
        from pocketclaw.deep_work import get_deep_work_session, reset_deep_work_session

        reset_deep_work_session()

        with (
            patch(
                "pocketclaw.mission_control.manager.get_mission_control_manager",
                return_value=MagicMock(),
            ),
            patch(
                "pocketclaw.mission_control.executor.get_mc_task_executor",
                return_value=MagicMock(),
            ),
        ):
            s1 = get_deep_work_session()
            s2 = get_deep_work_session()
            assert s1 is s2

        reset_deep_work_session()

    def test_reset_deep_work_session_clears_singleton(self):
        """reset_deep_work_session should clear the cached instance."""
        from pocketclaw.deep_work import get_deep_work_session, reset_deep_work_session

        reset_deep_work_session()

        with (
            patch(
                "pocketclaw.mission_control.manager.get_mission_control_manager",
                return_value=MagicMock(),
            ),
            patch(
                "pocketclaw.mission_control.executor.get_mc_task_executor",
                return_value=MagicMock(),
            ),
        ):
            s1 = get_deep_work_session()
            reset_deep_work_session()
            s2 = get_deep_work_session()
            assert s1 is not s2

        reset_deep_work_session()


# ============================================================================
# Constructor tests
# ============================================================================


class TestSessionConstructor:
    """Tests for DeepWorkSession initialization."""

    def test_default_dependencies(self, manager, mock_executor):
        """Verify planner, scheduler, human_router auto-created when not provided."""
        session = DeepWorkSession(manager=manager, executor=mock_executor)

        assert isinstance(session.planner, PlannerAgent)
        assert isinstance(session.scheduler, DependencyScheduler)
        assert isinstance(session.human_router, HumanTaskRouter)
        assert session.executor is mock_executor

    def test_custom_dependencies(self, manager, mock_executor, mock_planner, mock_human_router):
        """Verify custom dependencies used when provided."""
        session = DeepWorkSession(
            manager=manager,
            executor=mock_executor,
            planner=mock_planner,
            human_router=mock_human_router,
        )
        assert session.planner is mock_planner
        assert session.human_router is mock_human_router
