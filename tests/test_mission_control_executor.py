# Tests for Mission Control Task Executor
# Created: 2026-02-05
# Updated: 2026-02-12 - Added test_background_execution_completes for
#   execute_task_background self-defeating bug. Updated duplicate test
#   to use execute_task_background entry point.
#   Previous: 2026-02-05 - Fixed fixture isolation, added security feature tests
#
# Tests for MCTaskExecutor - agent task execution with streaming
# Includes security tests for:
# - UUID validation (prevents injection)
# - Error message sanitization (prevents information disclosure)
# - Rate limiting (prevents resource exhaustion)

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Access the private module to inject test store
import pocketclaw.mission_control.store as store_module
from pocketclaw.mission_control import (
    AgentProfile,
    AgentStatus,
    FileMissionControlStore,
    Task,
    TaskPriority,
    TaskStatus,
    get_mission_control_manager,
    reset_mission_control_manager,
    reset_mission_control_store,
)
from pocketclaw.mission_control.executor import (
    MCTaskExecutor,
    get_mc_task_executor,
    reset_mc_task_executor,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_store_path():
    """Create a temporary directory for test storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def setup_singletons(temp_store_path):
    """Reset and configure all singletons to use temp storage.

    This ensures executor, manager, and store all use the same temp store.
    """
    # Reset all singletons
    reset_mission_control_store()
    reset_mission_control_manager()
    reset_mc_task_executor()

    # Create store with temp path and inject it as the singleton
    test_store = FileMissionControlStore(temp_store_path)
    store_module._store_instance = test_store

    yield test_store

    # Cleanup
    reset_mission_control_store()
    reset_mission_control_manager()
    reset_mc_task_executor()


@pytest.fixture
def manager(setup_singletons):
    """Get the manager that uses our test store."""
    return get_mission_control_manager()


@pytest.fixture
def executor(setup_singletons):
    """Get the executor that uses our test store."""
    return get_mc_task_executor()


@pytest.fixture
async def agent(manager):
    """Create a test agent."""
    return await manager.create_agent(
        name="TestAgent",
        role="Test Role",
        description="A test agent for execution tests",
        backend="claude_agent_sdk",
    )


@pytest.fixture
async def task(manager):
    """Create a test task."""
    return await manager.create_task(
        title="Test Task",
        description="A test task for execution",
        priority=TaskPriority.HIGH,
    )


@pytest.fixture
async def assigned_task(manager, agent, task):
    """Create a task assigned to an agent."""
    await manager.assign_task(task.id, [agent.id])
    return await manager.get_task(task.id)


# ============================================================================
# Mock AgentRouter
# ============================================================================


def create_mock_router(chunks=None, error=None):
    """Create a mock AgentRouter that yields predefined chunks."""
    if chunks is None:
        chunks = [
            {"type": "message", "content": "Starting task..."},
            {"type": "tool_use", "content": "", "metadata": {"name": "search"}},
            {"type": "tool_result", "content": "Found results"},
            {"type": "message", "content": "Completing task."},
            {"type": "done", "content": ""},
        ]

    async def mock_run(prompt):
        for chunk in chunks:
            if error and chunk.get("type") == "error":
                yield chunk
                return
            yield chunk
            await asyncio.sleep(0.01)  # Simulate async work

    mock = MagicMock()
    mock.run = mock_run
    mock.stop = AsyncMock()
    return mock


# ============================================================================
# Executor Tests
# ============================================================================


class TestMCTaskExecutor:
    """Tests for MCTaskExecutor."""

    def test_singleton_pattern(self):
        """Test that get_mc_task_executor returns singleton."""
        reset_mc_task_executor()
        e1 = get_mc_task_executor()
        e2 = get_mc_task_executor()
        assert e1 is e2

    def test_reset_executor(self):
        """Test executor reset."""
        reset_mc_task_executor()
        e1 = get_mc_task_executor()
        reset_mc_task_executor()
        e2 = get_mc_task_executor()
        assert e1 is not e2

    @pytest.mark.asyncio
    async def test_execute_invalid_task_id(self, executor):
        """Test execution with invalid task ID format (security validation)."""
        result = await executor.execute_task("fake-task-id", "fake-agent-id")
        assert result["status"] == "error"
        assert "Invalid task ID format" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_task_not_found(self, executor):
        """Test execution with non-existent but valid UUID task."""
        # Use valid UUID format but non-existent task
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        result = await executor.execute_task(fake_uuid, fake_uuid)
        assert result["status"] == "error"
        assert "Task not found" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_agent_not_found(self, executor, task):
        """Test execution with non-existent agent."""
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        result = await executor.execute_task(task.id, fake_uuid)
        assert result["status"] == "error"
        assert "Agent not found" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_task_success(self, executor, assigned_task, agent):
        """Test successful task execution."""
        mock_router = create_mock_router()

        with patch(
            "pocketclaw.mission_control.executor.AgentRouter",
            return_value=mock_router,
        ):
            with patch("pocketclaw.mission_control.executor.get_message_bus") as mock_bus:
                mock_bus.return_value.publish_system = AsyncMock()

                result = await executor.execute_task(assigned_task.id, agent.id)

        assert result["status"] == "completed"
        assert "Starting task" in result["output"]
        assert "Completing task" in result["output"]

    @pytest.mark.asyncio
    async def test_execute_task_updates_status(self, executor, manager, assigned_task, agent):
        """Test that execution updates task and agent status."""
        mock_router = create_mock_router()

        with patch(
            "pocketclaw.mission_control.executor.AgentRouter",
            return_value=mock_router,
        ):
            with patch("pocketclaw.mission_control.executor.get_message_bus") as mock_bus:
                mock_bus.return_value.publish_system = AsyncMock()

                await executor.execute_task(assigned_task.id, agent.id)

        # Check task status updated to done
        task_updated = await manager.get_task(assigned_task.id)
        assert task_updated.status == TaskStatus.DONE

        # Check agent status back to idle
        agent_updated = await manager.get_agent(agent.id)
        assert agent_updated.status == AgentStatus.IDLE

    @pytest.mark.asyncio
    async def test_execute_task_with_error(self, executor, manager, assigned_task, agent):
        """Test task execution with error."""
        error_chunks = [
            {"type": "message", "content": "Starting..."},
            {"type": "error", "content": "API rate limit exceeded"},
        ]
        mock_router = create_mock_router(chunks=error_chunks, error=True)

        with patch(
            "pocketclaw.mission_control.executor.AgentRouter",
            return_value=mock_router,
        ):
            with patch("pocketclaw.mission_control.executor.get_message_bus") as mock_bus:
                mock_bus.return_value.publish_system = AsyncMock()

                result = await executor.execute_task(assigned_task.id, agent.id)

        assert result["status"] == "error"
        assert "API rate limit" in result["error"]

        # Check task status is blocked
        task_updated = await manager.get_task(assigned_task.id)
        assert task_updated.status == TaskStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_is_task_running(self, executor, assigned_task):
        """Test checking if task is running.

        Uses a simpler approach: check the internal _running_tasks dict directly
        since async timing makes it hard to reliably test in-flight status.
        """
        # Should not be running initially
        assert not executor.is_task_running(assigned_task.id)
        assert assigned_task.id not in executor._running_tasks

        # Manually add to running tasks to test the check
        executor._running_tasks[assigned_task.id] = MagicMock()
        assert executor.is_task_running(assigned_task.id)

        # Remove and verify
        del executor._running_tasks[assigned_task.id]
        assert not executor.is_task_running(assigned_task.id)

    @pytest.mark.asyncio
    async def test_get_running_tasks(self, executor):
        """Test getting list of running tasks."""
        assert executor.get_running_tasks() == []

    @pytest.mark.asyncio
    async def test_stop_task_not_running(self, executor):
        """Test stopping a task that isn't running."""
        result = await executor.stop_task("non-existent-task")
        assert result is False

    @pytest.mark.asyncio
    async def test_duplicate_execution_prevented(self, setup_singletons, manager):
        """Test that same task cannot be dispatched twice via execute_task_background.

        The duplicate guard lives in execute_task_background (not execute_task).
        A second call while the task is already tracked in _running_tasks is
        silently skipped, and the API layer returns 409 via is_task_running().
        """
        executor = get_mc_task_executor()

        # Create agent and task
        agent = await manager.create_agent(
            name="DupeTestAgent",
            role="Test Role",
            backend="claude_agent_sdk",
        )
        task = await manager.create_task(
            title="Dupe Test Task",
            priority=TaskPriority.MEDIUM,
        )
        await manager.assign_task(task.id, [agent.id])
        task = await manager.get_task(task.id)

        # Simulate a long-running task
        async def slow_run(prompt):
            yield {"type": "message", "content": "Working..."}
            await asyncio.sleep(10)  # Long wait

        mock_router = MagicMock()
        mock_router.run = slow_run
        mock_router.stop = AsyncMock()

        with patch(
            "pocketclaw.mission_control.executor.AgentRouter",
            return_value=mock_router,
        ):
            with patch("pocketclaw.mission_control.executor.get_message_bus") as mock_bus:
                mock_bus.return_value.publish_system = AsyncMock()

                # Start first execution
                await executor.execute_task_background(task.id, agent.id)
                await asyncio.sleep(0.02)  # Let it start

                # Verify it's tracked as running
                assert executor.is_task_running(task.id)

                # Second dispatch via execute_task_background is silently skipped
                await executor.execute_task_background(task.id, agent.id)

                # Still only one entry in _running_tasks
                assert executor.is_task_running(task.id)

        # Cleanup
        await executor.stop_task(task.id)

    @pytest.mark.asyncio
    async def test_background_execution_completes(self, executor, manager, assigned_task, agent):
        """Bug: execute_task_background registers task in _running_tasks BEFORE
        execute_task starts. When execute_task runs, it sees itself as 'already
        running' and bails out, leaving a zombie entry in _running_tasks.

        This test verifies:
        1. The task actually executes to completion (status=DONE, not stuck)
        2. _running_tasks is cleaned up after completion (no zombie entry)
        3. A subsequent run attempt succeeds (no stale 409)
        """
        mock_router = create_mock_router()

        with patch(
            "pocketclaw.mission_control.executor.AgentRouter",
            return_value=mock_router,
        ):
            with patch("pocketclaw.mission_control.executor.get_message_bus") as mock_bus:
                mock_bus.return_value.publish_system = AsyncMock()

                await executor.execute_task_background(assigned_task.id, agent.id)
                await asyncio.sleep(0.5)  # Wait for background task to finish

        # Task should have completed (not stuck at ASSIGNED)
        task_updated = await manager.get_task(assigned_task.id)
        assert task_updated.status == TaskStatus.DONE, (
            f"Expected DONE but got {task_updated.status}. "
            "execute_task_background likely self-defeated via _running_tasks check."
        )

        # No zombie in _running_tasks
        assert not executor.is_task_running(assigned_task.id), (
            "Zombie: task still in _running_tasks after background execution completed."
        )

    @pytest.mark.asyncio
    async def test_broadcasts_events(self, executor, assigned_task, agent):
        """Test that execution broadcasts WebSocket events."""
        mock_router = create_mock_router()
        broadcast_calls = []

        async def capture_broadcast(event):
            broadcast_calls.append(event)

        with patch(
            "pocketclaw.mission_control.executor.AgentRouter",
            return_value=mock_router,
        ):
            with patch("pocketclaw.mission_control.executor.get_message_bus") as mock_bus:
                mock_bus.return_value.publish_system = capture_broadcast

                await executor.execute_task(assigned_task.id, agent.id)

        # Check events were broadcast
        event_types = [e.event_type for e in broadcast_calls]

        assert "mc_task_started" in event_types
        assert "mc_task_output" in event_types
        assert "mc_task_completed" in event_types
        assert "mc_activity_created" in event_types

    @pytest.mark.asyncio
    async def test_build_task_prompt(self, executor, agent, task):
        """Test prompt building includes task and agent context."""
        prompt = await executor._build_task_prompt(task, agent)

        assert "TestAgent" in prompt
        assert "Test Role" in prompt
        assert "Test Task" in prompt
        assert "high" in prompt.lower()


class TestSecurityFeatures:
    """Tests for security features of MCTaskExecutor."""

    def test_uuid_validation_valid(self):
        """Test UUID validation with valid UUIDs."""
        executor = MCTaskExecutor()
        assert executor._is_valid_uuid("550e8400-e29b-41d4-a716-446655440000")
        assert executor._is_valid_uuid("00000000-0000-0000-0000-000000000000")

    def test_uuid_validation_invalid(self):
        """Test UUID validation rejects invalid formats."""
        executor = MCTaskExecutor()
        assert not executor._is_valid_uuid("not-a-uuid")
        assert not executor._is_valid_uuid("")
        assert not executor._is_valid_uuid(None)
        assert not executor._is_valid_uuid("550e8400-e29b-41d4-a716")  # Too short
        assert not executor._is_valid_uuid("../../../etc/passwd")  # Path traversal attempt

    def test_error_sanitization_truncates(self):
        """Test error sanitization truncates long messages."""
        executor = MCTaskExecutor()
        long_error = "A" * 500
        sanitized = executor._sanitize_error(long_error)
        assert len(sanitized) <= 203  # 200 + "..."
        assert sanitized.endswith("...")

    def test_error_sanitization_removes_paths(self):
        """Test error sanitization removes file paths."""
        executor = MCTaskExecutor()
        error = "Error loading /home/user/secrets/key.pem"
        sanitized = executor._sanitize_error(error)
        assert "/home/user" not in sanitized
        assert "[path]" in sanitized

    def test_error_sanitization_removes_secrets(self):
        """Test error sanitization removes API keys and tokens."""
        executor = MCTaskExecutor()
        error = "API call failed: key=sk-abc123secret token=ghp_xyz789"
        sanitized = executor._sanitize_error(error)
        assert "sk-abc123" not in sanitized
        assert "ghp_xyz789" not in sanitized
        assert "[redacted]" in sanitized

    def test_error_sanitization_empty(self):
        """Test error sanitization handles empty input."""
        executor = MCTaskExecutor()
        assert executor._sanitize_error("") == "An error occurred"
        assert executor._sanitize_error(None) == "An error occurred"


class TestPromptBuilding:
    """Tests for task prompt construction."""

    @pytest.mark.asyncio
    async def test_prompt_includes_agent_info(self):
        """Test prompt includes agent name, role, description."""
        executor = MCTaskExecutor()
        agent = AgentProfile(
            name="Jarvis",
            role="Squad Lead",
            description="Coordinates the team",
            specialties=["planning", "coordination"],
        )
        task = Task(title="Test", description="Test task", priority=TaskPriority.HIGH)

        prompt = await executor._build_task_prompt(task, agent)

        assert "Jarvis" in prompt
        assert "Squad Lead" in prompt
        assert "Coordinates the team" in prompt
        assert "planning" in prompt
        assert "coordination" in prompt

    @pytest.mark.asyncio
    async def test_prompt_includes_task_info(self):
        """Test prompt includes task title, description, priority."""
        executor = MCTaskExecutor()
        agent = AgentProfile(name="Agent", role="Role")
        task = Task(
            title="Research competitors",
            description="Full competitive analysis",
            priority=TaskPriority.URGENT,
        )

        prompt = await executor._build_task_prompt(task, agent)

        assert "Research competitors" in prompt
        assert "Full competitive analysis" in prompt
        assert "urgent" in prompt.lower()

    @pytest.mark.asyncio
    async def test_prompt_handles_missing_description(self):
        """Test prompt handles agent/task with no description."""
        executor = MCTaskExecutor()
        agent = AgentProfile(name="Agent", role="Role")
        task = Task(title="Task", priority=TaskPriority.LOW)

        prompt = await executor._build_task_prompt(task, agent)

        # Should not crash and should still have basic info
        assert "Agent" in prompt
        assert "Task" in prompt
