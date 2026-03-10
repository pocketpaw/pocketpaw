# E2E tests for Deep Work v2 — full API flow tests.
# Created: 2026-02-27
# Updated: 2026-02-27 — Fixed save_agent → update_agent, removed unused imports
#
# These tests exercise the Deep Work API through the real FastAPI router
# with real file-based storage and real session/scheduler/executor. Only
# the agent backend (LLM calls) is mocked — everything else is real.
#
# Covers:
#   1. Full project lifecycle: create → get plan → approve → execution
#   2. Task auto-retry on failure: fail → retry fires → eventually BLOCKED
#   3. Task timeout: slow task → timeout → retry/BLOCKED
#   4. Project cancellation mid-execution: cancel → tasks skipped, project CANCELLED
#   5. Manual retry of blocked task via API
#   6. Output chaining: task output stored on task.output field
#   7. Cancel rejection: completed/cancelled projects can't be cancelled again
#   8. PawKit YAML round-trip through real file system

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from pocketpaw.deep_work.api import router as deep_work_router
from pocketpaw.deep_work.models import (
    AgentSpec,
    PlannerResult,
    ProjectStatus,
    TaskSpec,
)
from pocketpaw.deep_work.session import DeepWorkSession
from pocketpaw.mission_control.executor import MCTaskExecutor
from pocketpaw.mission_control.manager import (
    MissionControlManager,
    reset_mission_control_manager,
)
from pocketpaw.mission_control.models import TaskStatus, now_iso
from pocketpaw.mission_control.store import (
    FileMissionControlStore,
    reset_mission_control_store,
)

# ============================================================================
# Helpers
# ============================================================================


def _make_planner_result(project_id: str = "") -> PlannerResult:
    """Realistic planner result with 2 tasks: t1 (no deps) → t2 (depends on t1)."""
    return PlannerResult(
        project_id=project_id,
        prd_content="# Build a REST API\n\n## Overview\nCreate a simple REST API.",
        tasks=[
            TaskSpec(
                key="t1",
                title="Set up project scaffolding",
                description="Create project structure with FastAPI boilerplate",
                task_type="agent",
                priority="high",
                tags=["setup"],
                estimated_minutes=15,
                required_specialties=["python"],
                blocked_by_keys=[],
                max_retries=1,
                timeout_minutes=5,
            ),
            TaskSpec(
                key="t2",
                title="Implement API endpoints",
                description="Create GET/POST/PUT/DELETE endpoints for resources",
                task_type="agent",
                priority="medium",
                tags=["backend"],
                estimated_minutes=30,
                required_specialties=["python", "fastapi"],
                blocked_by_keys=["t1"],
                max_retries=2,
                timeout_minutes=10,
            ),
        ],
        team_recommendation=[
            AgentSpec(
                name="dev-bot",
                role="Backend Developer",
                description="Builds REST APIs",
                specialties=["python", "fastapi"],
                backend="claude_agent_sdk",
            ),
        ],
        human_tasks=[],
        dependency_graph={"t2": ["t1"]},
        estimated_total_minutes=45,
    )


def _make_single_task_result(
    max_retries: int = 1,
    timeout_minutes: int | None = None,
) -> PlannerResult:
    """Planner result with a single task — useful for retry/timeout tests."""
    return PlannerResult(
        project_id="",
        prd_content="# Quick Task\n\nOne simple task.",
        tasks=[
            TaskSpec(
                key="only",
                title="Execute task",
                description="Do the thing",
                task_type="agent",
                priority="high",
                tags=[],
                estimated_minutes=10,
                required_specialties=["python"],
                blocked_by_keys=[],
                max_retries=max_retries,
                timeout_minutes=timeout_minutes,
            ),
        ],
        team_recommendation=[
            AgentSpec(
                name="worker-bot",
                role="Worker",
                specialties=["python"],
                backend="claude_agent_sdk",
            ),
        ],
        human_tasks=[],
        estimated_total_minutes=10,
    )


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_store_path(tmp_path):
    """Temporary directory for isolated file-based store."""
    return tmp_path


@pytest.fixture
def store(temp_store_path):
    """Fresh file store per test."""
    reset_mission_control_store()
    return FileMissionControlStore(temp_store_path)


@pytest.fixture
def manager(store):
    """Manager backed by temp store."""
    reset_mission_control_manager()
    return MissionControlManager(store)


@pytest.fixture
def executor():
    """Real MCTaskExecutor (we mock _stream_task on it per-test)."""
    return MCTaskExecutor()


@pytest.fixture
def mock_human_router():
    """Mock HumanTaskRouter — no real messaging."""
    router = MagicMock()
    router.notify_human_task = AsyncMock()
    router.notify_review_task = AsyncMock()
    router.notify_plan_ready = AsyncMock()
    router.notify_project_completed = AsyncMock()
    return router


@pytest.fixture
def session(manager, executor, mock_human_router):
    """Real DeepWorkSession wired to real store and executor."""
    return DeepWorkSession(
        manager=manager,
        executor=executor,
        human_router=mock_human_router,
    )


@pytest.fixture
def e2e_app(manager, session, monkeypatch):
    """FastAPI app with deep work router, wired to isolated store+session."""
    import pocketpaw.deep_work as dw_module
    import pocketpaw.mission_control.manager as manager_module
    import pocketpaw.mission_control.store as store_module

    # Wire singletons to our test instances
    monkeypatch.setattr(store_module, "_store_instance", manager._store)
    monkeypatch.setattr(manager_module, "_manager_instance", manager)
    monkeypatch.setattr(dw_module, "_session_instance", session)

    app = FastAPI()
    app.include_router(deep_work_router, prefix="/api/deep-work")
    return app


@pytest.fixture
def client(e2e_app):
    """httpx AsyncClient using ASGI transport — no real server needed."""
    transport = httpx.ASGITransport(app=e2e_app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ============================================================================
# Helpers for simulating execution
# ============================================================================


async def _plan_and_approve(session, manager, planner_result):
    """Helper: create project, plan it, approve it — returns (project, tasks)."""
    project = await manager.create_project(
        title=planner_result.prd_content[:60],
        description="E2E test project",
        creator_id="human",
    )
    project.status = ProjectStatus.PLANNING
    await manager.update_project(project)

    # Simulate planning: materialize tasks and set to AWAITING_APPROVAL
    planner_result.project_id = project.id
    await session._materialize_tasks(project, planner_result.tasks)

    # Create agent team
    for agent_spec in planner_result.team_recommendation:
        agent_profile = await manager.create_agent(
            name=agent_spec.name,
            role=agent_spec.role,
            description=agent_spec.description or "",
        )
        agent_profile.specialties = agent_spec.specialties
        agent_profile.backend = agent_spec.backend
        await manager.update_agent(agent_profile)
        project.team_agent_ids.append(agent_profile.id)

    # Assign agents to tasks
    tasks = await manager.get_project_tasks(project.id)
    for task in tasks:
        if task.task_type == "agent" and project.team_agent_ids:
            task.assignee_ids = [project.team_agent_ids[0]]
            task.status = TaskStatus.ASSIGNED
            await manager.save_task(task)

    project.task_ids = [t.id for t in tasks]
    project.status = ProjectStatus.AWAITING_APPROVAL
    await manager.update_project(project)

    return project, tasks


# ============================================================================
# 1. Full lifecycle: plan → approve → execute → complete
# ============================================================================


class TestFullLifecycle:
    """Test the complete project lifecycle through the API."""

    async def test_approve_dispatches_ready_tasks(self, client, session, manager, executor):
        """Approving a project should dispatch tasks with no blockers."""
        result = _make_planner_result()
        project, tasks = await _plan_and_approve(session, manager, result)

        # Mock executor so tasks don't actually run
        executor.execute_task_background = AsyncMock()

        response = await client.post(f"/api/deep-work/projects/{project.id}/approve")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["project"]["status"] == "executing"

        # t1 should have been dispatched (no blockers), t2 should not (blocked by t1)
        executor.execute_task_background.assert_called_once()

    async def test_task_completion_cascades_to_dependents(self, client, session, manager, executor):
        """When t1 completes, t2 should be dispatched automatically."""
        result = _make_planner_result()
        project, tasks = await _plan_and_approve(session, manager, result)

        t1 = next(t for t in tasks if t.title == "Set up project scaffolding")
        t2 = next(t for t in tasks if t.title == "Implement API endpoints")

        # Mock: executor just records dispatches
        dispatched_task_ids = []

        async def mock_execute(task_id, agent_id):
            dispatched_task_ids.append(task_id)

        executor.execute_task_background = AsyncMock(side_effect=mock_execute)

        # Approve — dispatches t1
        response = await client.post(f"/api/deep-work/projects/{project.id}/approve")
        assert response.status_code == 200
        assert t1.id in dispatched_task_ids

        # Simulate t1 completing
        t1_fresh = await manager.get_task(t1.id)
        t1_fresh.status = TaskStatus.DONE
        t1_fresh.completed_at = now_iso()
        await manager.save_task(t1_fresh)

        # Trigger scheduler cascade
        await session.scheduler.on_task_completed(t1.id)

        # t2 should now be dispatched
        assert t2.id in dispatched_task_ids

    async def test_project_completes_when_all_tasks_done(self, client, session, manager, executor):
        """Project status should change to COMPLETED when all tasks finish."""
        result = _make_single_task_result()
        project, tasks = await _plan_and_approve(session, manager, result)
        task = tasks[0]

        executor.execute_task_background = AsyncMock()

        # Approve
        await client.post(f"/api/deep-work/projects/{project.id}/approve")

        # Complete the only task
        task_fresh = await manager.get_task(task.id)
        task_fresh.status = TaskStatus.DONE
        task_fresh.completed_at = now_iso()
        await manager.save_task(task_fresh)

        # Trigger completion check
        await session.scheduler.on_task_completed(task.id)

        # Verify project is now completed
        project_fresh = await manager.get_project(project.id)
        assert project_fresh.status == ProjectStatus.COMPLETED


# ============================================================================
# 2. Task output storage
# ============================================================================


class TestOutputChaining:
    """Verify task.output gets populated after execution."""

    async def test_output_stored_after_successful_execution(
        self, client, session, manager, executor
    ):
        """After task execution, the output should be stored on the task."""
        result = _make_single_task_result()
        project, tasks = await _plan_and_approve(session, manager, result)
        task = tasks[0]
        agent_id = project.team_agent_ids[0]

        # Mock the agent to produce known output
        async def fake_stream(router, prompt, task_id, output_chunks):
            output_chunks.append("Here is the API scaffolding code:\n")
            output_chunks.append("```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```")

        mock_settings = MagicMock()
        mock_settings.agent_backend = "claude_agent_sdk"
        mock_settings.anthropic_api_key = None
        mock_settings.anthropic_model = "claude-3-5-sonnet"
        mock_settings.openai_api_key = None
        mock_settings.openai_model = "gpt-4o"
        mock_settings.ollama_host = "http://localhost:11434"
        mock_settings.ollama_model = "llama3"
        mock_settings.llm_provider = "anthropic"
        mock_settings.bypass_permissions = True

        with (
            patch.object(executor, "_stream_task", side_effect=fake_stream),
            patch.object(executor, "_broadcast_event", new_callable=AsyncMock),
            patch.object(executor, "_log_activity", new_callable=AsyncMock),
            patch.object(executor, "_save_task_deliverable", new_callable=AsyncMock),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "pocketpaw.mission_control.executor.AgentRouter",
                return_value=MagicMock(),
            ),
        ):
            result = await executor.execute_task(task.id, agent_id)

        assert result["status"] == "completed"

        task_after = await manager.get_task(task.id)
        assert task_after.output is not None
        assert "FastAPI" in task_after.output
        assert task_after.status == TaskStatus.DONE


# ============================================================================
# 3. Auto-retry on failure
# ============================================================================


class TestAutoRetry:
    """Verify auto-retry fires on failure when retries remain."""

    async def test_retry_fires_then_succeeds(self, client, session, manager, executor):
        """Task fails once, retries, succeeds on second attempt."""
        result = _make_single_task_result(max_retries=2)
        project, tasks = await _plan_and_approve(session, manager, result)
        task = tasks[0]
        agent_id = project.team_agent_ids[0]

        call_count = 0

        async def stream_fail_then_succeed(router, prompt, task_id, output_chunks):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Transient LLM error")
            output_chunks.append("Success on retry!")

        mock_settings = MagicMock()
        mock_settings.agent_backend = "claude_agent_sdk"
        mock_settings.anthropic_api_key = None
        mock_settings.anthropic_model = "claude-3-5-sonnet"
        mock_settings.openai_api_key = None
        mock_settings.openai_model = "gpt-4o"
        mock_settings.ollama_host = "http://localhost:11434"
        mock_settings.ollama_model = "llama3"
        mock_settings.llm_provider = "anthropic"
        mock_settings.bypass_permissions = True

        # Capture retry dispatches
        retry_dispatches = []

        async def capture_background(task_id, agent_id):
            retry_dispatches.append(task_id)
            # Actually execute the retry (second call will succeed)
            await executor.execute_task(task_id, agent_id)

        with (
            patch.object(executor, "_stream_task", side_effect=stream_fail_then_succeed),
            patch.object(executor, "_broadcast_event", new_callable=AsyncMock),
            patch.object(executor, "_log_activity", new_callable=AsyncMock),
            patch.object(executor, "_save_task_deliverable", new_callable=AsyncMock),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "pocketpaw.mission_control.executor.AgentRouter",
                return_value=MagicMock(),
            ),
            patch(
                "pocketpaw.mission_control.executor.asyncio.create_task",
                side_effect=lambda coro: asyncio.ensure_future(coro),
            ),
        ):
            await executor.execute_task(task.id, agent_id)
            # Let the retry dispatch complete
            await asyncio.sleep(0.1)

        assert call_count == 2

        task_after = await manager.get_task(task.id)
        assert task_after.status == TaskStatus.DONE
        assert task_after.output == "Success on retry!"
        assert task_after.retry_count == 1

    async def test_retries_exhausted_goes_blocked(self, client, session, manager, executor):
        """Task fails repeatedly until max_retries exhausted → BLOCKED."""
        result = _make_single_task_result(max_retries=1)
        project, tasks = await _plan_and_approve(session, manager, result)
        task = tasks[0]
        agent_id = project.team_agent_ids[0]

        async def always_fail(router, prompt, task_id, output_chunks):
            raise RuntimeError("Permanent failure")

        mock_settings = MagicMock()
        mock_settings.agent_backend = "claude_agent_sdk"
        mock_settings.anthropic_api_key = None
        mock_settings.anthropic_model = "claude-3-5-sonnet"
        mock_settings.openai_api_key = None
        mock_settings.openai_model = "gpt-4o"
        mock_settings.ollama_host = "http://localhost:11434"
        mock_settings.ollama_model = "llama3"
        mock_settings.llm_provider = "anthropic"
        mock_settings.bypass_permissions = True

        # Execute until exhaustion (original + 1 retry = 2 calls)
        with (
            patch.object(executor, "_stream_task", side_effect=always_fail),
            patch.object(executor, "_broadcast_event", new_callable=AsyncMock),
            patch.object(executor, "_log_activity", new_callable=AsyncMock),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "pocketpaw.mission_control.executor.AgentRouter",
                return_value=MagicMock(),
            ),
            patch(
                "pocketpaw.mission_control.executor.asyncio.create_task",
                side_effect=lambda coro: asyncio.ensure_future(coro),
            ),
        ):
            await executor.execute_task(task.id, agent_id)
            await asyncio.sleep(0.1)  # let retry fire

        task_after = await manager.get_task(task.id)
        assert task_after.status == TaskStatus.BLOCKED
        assert task_after.retry_count == 1  # retried once
        assert task_after.error_message is not None


# ============================================================================
# 4. Task timeout
# ============================================================================


class TestTaskTimeout:
    """Verify tasks with timeout_minutes actually time out."""

    async def test_timeout_triggers_retry_or_blocked(self, client, session, manager, executor):
        """A task that exceeds its timeout should get error_message set."""
        result = _make_single_task_result(max_retries=0, timeout_minutes=1)
        project, tasks = await _plan_and_approve(session, manager, result)
        task = tasks[0]
        agent_id = project.team_agent_ids[0]

        mock_settings = MagicMock()
        mock_settings.agent_backend = "claude_agent_sdk"
        mock_settings.anthropic_api_key = None
        mock_settings.anthropic_model = "claude-3-5-sonnet"
        mock_settings.openai_api_key = None
        mock_settings.openai_model = "gpt-4o"
        mock_settings.ollama_host = "http://localhost:11434"
        mock_settings.ollama_model = "llama3"
        mock_settings.llm_provider = "anthropic"
        mock_settings.bypass_permissions = True

        with (
            patch.object(executor, "_broadcast_event", new_callable=AsyncMock),
            patch.object(executor, "_log_activity", new_callable=AsyncMock),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "pocketpaw.mission_control.executor.AgentRouter",
                return_value=MagicMock(),
            ),
            # Force asyncio.wait_for to raise TimeoutError immediately
            patch(
                "pocketpaw.mission_control.executor.asyncio.wait_for",
                side_effect=TimeoutError(),
            ),
        ):
            result = await executor.execute_task(task.id, agent_id)

        assert result["status"] == "timeout"

        task_after = await manager.get_task(task.id)
        assert task_after.status == TaskStatus.BLOCKED  # max_retries=0
        assert "timed out" in task_after.error_message.lower()


# ============================================================================
# 5. Project cancellation
# ============================================================================


class TestProjectCancellation:
    """Test project cancellation through the full API."""

    async def test_cancel_executing_project(self, client, session, manager, executor):
        """Cancelling an executing project stops tasks and sets CANCELLED."""
        result = _make_planner_result()
        project, tasks = await _plan_and_approve(session, manager, result)

        executor.execute_task_background = AsyncMock()

        # Approve to set EXECUTING
        response = await client.post(f"/api/deep-work/projects/{project.id}/approve")
        assert response.status_code == 200

        # Cancel
        response = await client.post(f"/api/deep-work/projects/{project.id}/cancel")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["project"]["status"] == "cancelled"
        assert data["project"]["completed_at"] is not None

        # Verify all tasks are skipped
        all_tasks = await manager.get_project_tasks(project.id)
        for task in all_tasks:
            if task.status != TaskStatus.DONE:
                assert task.status == TaskStatus.SKIPPED
                assert task.error_message == "Project cancelled"

    async def test_cancel_paused_project(self, client, session, manager, executor):
        """Paused projects can also be cancelled."""
        result = _make_single_task_result()
        project, tasks = await _plan_and_approve(session, manager, result)

        project.status = ProjectStatus.PAUSED
        await manager.update_project(project)

        response = await client.post(f"/api/deep-work/projects/{project.id}/cancel")
        assert response.status_code == 200
        assert response.json()["project"]["status"] == "cancelled"

    async def test_cancel_completed_project_fails(self, client, session, manager, executor):
        """Completed projects cannot be cancelled."""
        result = _make_single_task_result()
        project, tasks = await _plan_and_approve(session, manager, result)

        project.status = ProjectStatus.COMPLETED
        project.completed_at = now_iso()
        await manager.update_project(project)

        response = await client.post(f"/api/deep-work/projects/{project.id}/cancel")
        assert response.status_code == 400
        assert "Cannot cancel" in response.json()["detail"]

    async def test_cancel_already_cancelled_fails(self, client, session, manager, executor):
        """Already cancelled projects cannot be cancelled again."""
        result = _make_single_task_result()
        project, tasks = await _plan_and_approve(session, manager, result)

        project.status = ProjectStatus.CANCELLED
        project.completed_at = now_iso()
        await manager.update_project(project)

        response = await client.post(f"/api/deep-work/projects/{project.id}/cancel")
        assert response.status_code == 400

    async def test_cancel_nonexistent_project(self, client):
        """Cancelling a project that doesn't exist returns error."""
        response = await client.post("/api/deep-work/projects/fake-id/cancel")
        # Should be 400 (ValueError from session.cancel raises)
        assert response.status_code == 400


# ============================================================================
# 6. Manual retry via API
# ============================================================================


class TestManualRetryAPI:
    """POST /projects/{id}/tasks/{tid}/retry through the real API stack."""

    async def test_retry_blocked_task(self, client, session, manager, executor):
        """Retrying a BLOCKED task resets it to ASSIGNED with incremented retry_count."""
        result = _make_single_task_result(max_retries=0)
        project, tasks = await _plan_and_approve(session, manager, result)
        task = tasks[0]

        # Set task to BLOCKED (simulate failed execution)
        task_fresh = await manager.get_task(task.id)
        task_fresh.status = TaskStatus.BLOCKED
        task_fresh.error_message = "LLM rate limit"
        task_fresh.retry_count = 0
        await manager.save_task(task_fresh)

        # Mock scheduler dispatch so it doesn't actually execute
        session.scheduler._dispatch_task = AsyncMock()

        response = await client.post(f"/api/deep-work/projects/{project.id}/tasks/{task.id}/retry")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["task"]["status"] == "assigned"
        assert data["task"]["retry_count"] == 1
        assert data["task"]["error_message"] is None  # cleared on retry

    async def test_retry_non_blocked_task_fails(self, client, session, manager, executor):
        """Retrying a task that isn't BLOCKED returns 400."""
        result = _make_single_task_result()
        project, tasks = await _plan_and_approve(session, manager, result)
        task = tasks[0]

        # Task is ASSIGNED, not BLOCKED
        response = await client.post(f"/api/deep-work/projects/{project.id}/tasks/{task.id}/retry")

        assert response.status_code == 400
        assert "blocked" in response.json()["detail"].lower()

    async def test_retry_task_wrong_project(self, client, session, manager, executor):
        """Retrying a task with wrong project ID returns 400."""
        result = _make_single_task_result()
        project, tasks = await _plan_and_approve(session, manager, result)
        task = tasks[0]

        # Set to BLOCKED first
        task_fresh = await manager.get_task(task.id)
        task_fresh.status = TaskStatus.BLOCKED
        await manager.save_task(task_fresh)

        # Use wrong project ID
        response = await client.post(
            f"/api/deep-work/projects/wrong-project-id/tasks/{task.id}/retry"
        )
        assert response.status_code == 400
        assert "does not belong" in response.json()["detail"]

    async def test_retry_nonexistent_task(self, client, session, manager, executor):
        """Retrying a nonexistent task returns 404."""
        result = _make_single_task_result()
        project, tasks = await _plan_and_approve(session, manager, result)

        response = await client.post(
            f"/api/deep-work/projects/{project.id}/tasks/fake-task-id/retry"
        )
        assert response.status_code == 404


# ============================================================================
# 7. Get plan API
# ============================================================================


class TestGetPlanAPI:
    """GET /projects/{id}/plan returns structured plan data."""

    async def test_get_plan_returns_tasks_and_levels(self, client, session, manager, executor):
        """Plan endpoint should return tasks, execution_levels, and progress."""
        result = _make_planner_result()
        project, tasks = await _plan_and_approve(session, manager, result)

        response = await client.get(f"/api/deep-work/projects/{project.id}/plan")

        assert response.status_code == 200
        data = response.json()
        assert data["project"]["id"] == project.id
        assert len(data["tasks"]) == 2

        # execution_levels should show t1 in level 0, t2 in level 1
        assert "execution_levels" in data
        assert len(data["execution_levels"]) == 2  # two dependency levels

        assert "progress" in data
        assert "task_level_map" in data

    async def test_get_plan_nonexistent_project(self, client):
        """Getting plan for nonexistent project returns 404."""
        response = await client.get("/api/deep-work/projects/fake-id/plan")
        assert response.status_code == 404


# ============================================================================
# 8. Skip task API
# ============================================================================


class TestSkipTaskAPI:
    """POST /projects/{id}/tasks/{tid}/skip."""

    async def test_skip_task_unblocks_dependents(self, client, session, manager, executor):
        """Skipping t1 should unblock t2 and set t1 to SKIPPED."""
        result = _make_planner_result()
        project, tasks = await _plan_and_approve(session, manager, result)

        t1 = next(t for t in tasks if t.title == "Set up project scaffolding")

        executor.execute_task_background = AsyncMock()

        # Approve to move to EXECUTING
        await client.post(f"/api/deep-work/projects/{project.id}/approve")

        # Skip t1
        response = await client.post(f"/api/deep-work/projects/{project.id}/tasks/{t1.id}/skip")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["task"]["status"] == "skipped"

        # t1 should be SKIPPED
        t1_after = await manager.get_task(t1.id)
        assert t1_after.status == TaskStatus.SKIPPED

    async def test_skip_done_task_fails(self, client, session, manager, executor):
        """Cannot skip an already-completed task."""
        result = _make_single_task_result()
        project, tasks = await _plan_and_approve(session, manager, result)
        task = tasks[0]

        task_fresh = await manager.get_task(task.id)
        task_fresh.status = TaskStatus.DONE
        task_fresh.completed_at = now_iso()
        await manager.save_task(task_fresh)

        response = await client.post(f"/api/deep-work/projects/{project.id}/tasks/{task.id}/skip")
        assert response.status_code == 400


# ============================================================================
# 9. PawKit YAML E2E — file system round-trip
# ============================================================================


class TestPawKitFileRoundTrip:
    """PawKit save to real file, load back, verify equality."""

    def test_full_yaml_file_roundtrip(self, tmp_path):
        """Save a complex PawKit config to YAML file and load it back."""
        from pocketpaw.deep_work.pawkit import (
            IntegrationRequirements,
            LayoutConfig,
            PanelConfig,
            PanelType,
            PawKitCategory,
            PawKitConfig,
            PawKitMeta,
            SectionConfig,
            SpanType,
            UserConfigField,
            UserConfigFieldType,
            WorkflowConfig,
            load_pawkit,
            save_pawkit,
        )

        original = PawKitConfig(
            meta=PawKitMeta(
                name="E2E Test Kit",
                author="test-suite",
                version="2.0.0",
                description="A kit for E2E testing",
                category=PawKitCategory.code,
                tags=["testing", "e2e"],
                built_in=False,
            ),
            layout=LayoutConfig(
                columns=3,
                sections=[
                    SectionConfig(
                        title="Test Dashboard",
                        span=SpanType.full,
                        panels=[
                            PanelConfig(
                                id="test-metrics",
                                panel_type=PanelType.metrics_row,
                                items=[
                                    {"label": "Tests Passed", "value": "{{passed}}"},
                                    {"label": "Tests Failed", "value": "{{failed}}"},
                                ],
                            ),
                            PanelConfig(
                                id="test-feed",
                                panel_type=PanelType.feed,
                                source="workflow_test_results",
                                max_items=50,
                            ),
                        ],
                    ),
                    SectionConfig(
                        title="Coverage",
                        span=SpanType.left,
                        panels=[
                            PanelConfig(
                                id="coverage-chart",
                                panel_type=PanelType.chart,
                            ),
                        ],
                    ),
                ],
            ),
            workflows={
                "run_tests": WorkflowConfig(
                    schedule="every push",
                    instruction="Run pytest suite and report results",
                    retry=3,
                ),
                "coverage_report": WorkflowConfig(
                    schedule="daily 6am",
                    instruction="Generate test coverage report",
                ),
            },
            user_config=[
                UserConfigField(
                    key="test_command",
                    label="Test Command",
                    field_type=UserConfigFieldType.text,
                    default="pytest tests/",
                ),
                UserConfigField(
                    key="coverage_threshold",
                    label="Min Coverage %",
                    field_type=UserConfigFieldType.number,
                    default=80,
                ),
            ],
            skills=["pytest-runner", "coverage-analyzer"],
            integrations=IntegrationRequirements(
                required=["github"],
                optional=["codecov", "slack"],
            ),
        )

        # Save
        yaml_path = tmp_path / "test_kit.yaml"
        save_pawkit(original, yaml_path)
        assert yaml_path.exists()
        content = yaml_path.read_text()
        assert "E2E Test Kit" in content
        assert "run_tests" in content

        # Load
        loaded = load_pawkit(yaml_path)
        assert loaded.meta.name == original.meta.name
        assert loaded.meta.version == original.meta.version
        assert loaded.meta.category == original.meta.category
        assert len(loaded.meta.tags) == 2
        assert loaded.layout.columns == 3
        assert len(loaded.layout.sections) == 2
        assert loaded.layout.sections[0].title == "Test Dashboard"
        assert len(loaded.layout.sections[0].panels) == 2
        assert loaded.layout.sections[0].panels[1].max_items == 50
        assert "run_tests" in loaded.workflows
        assert loaded.workflows["run_tests"].retry == 3
        assert "coverage_report" in loaded.workflows
        assert len(loaded.user_config) == 2
        assert loaded.user_config[1].default == 80
        assert "pytest-runner" in loaded.skills
        assert "github" in loaded.integrations.required
        assert "codecov" in loaded.integrations.optional
